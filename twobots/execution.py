from __future__ import annotations
import copy
import json
import math
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from .storage import dumps


def quantize(value, step, up=False):
    v, s = Decimal(str(value)), Decimal(str(step))
    if not v.is_finite() or not s.is_finite() or s <= 0 or v < 0:
        raise ValueError("Invalid quantity/price increment")
    return float((v/s).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*s)


class Book:
    """Aggregated L2 book. Diff sequence continuity is mandatory."""
    def __init__(self):
        self.bids, self.asks = {}, {}
        self.update_id, self.received, self.valid = 0, 0.0, False
        self.consumed = {}

    def snapshot(self, data, received=None):
        self.bids = {float(p):float(q) for p,q in data["bids"] if float(q)>0}
        self.asks = {float(p):float(q) for p,q in data["asks"] if float(q)>0}
        self.update_id = int(data["lastUpdateId"])
        self.received = received or time.time()
        self.valid = bool(self.bids and self.asks) and max(self.bids)<min(self.asks)
        self.consumed.clear()

    def apply(self, event, received=None):
        if int(event["u"]) <= self.update_id:
            return True
        if not self.valid or not int(event["U"]) <= self.update_id+1 <= int(event["u"]):
            self.valid = False
            return False
        for side, key in ((self.bids,"b"),(self.asks,"a")):
            for p,q in event[key]:
                price, qty = float(p), float(q)
                if qty == 0:
                    side.pop(price, None)
                else:
                    side[price] = qty
                # A venue update can replenish or cancel this level.
                self.consumed.pop((key, price), None)
        self.update_id = int(event["u"])
        self.received = received or time.time()
        self.valid = bool(self.bids and self.asks) and max(self.bids)<min(self.asks)
        return self.valid

    def fresh(self, maximum_ms, now=None):
        return self.valid and 0 <= (now or time.time())-self.received <= maximum_ms/1000

    def top(self, side):
        return min(self.asks) if side == "BUY" and self.asks else (max(self.bids) if self.bids else None)

    def ioc(self, side, qty, limit_price, liquidity_fraction, adverse_bps, step):
        """Only known displayed liquidity, with configurable availability haircut.

        Unobserved depth is never fabricated. Limit boundary is respected even
        after stress. No maker/queue fills or hidden-liquidity claims.
        """
        levels, key = (self.asks,"a") if side == "BUY" else (self.bids,"b")
        remain, fills = qty, []
        for price in sorted(levels, reverse=side=="SELL"):
            stressed = price * (1 + adverse_bps/10000 * (1 if side=="BUY" else -1))
            if (side=="BUY" and stressed>limit_price) or (side=="SELL" and stressed<limit_price):
                break
            available = max(0, levels[price]*liquidity_fraction-self.consumed.get((key,price),0))
            amount = quantize(min(remain, available), step)
            if amount:
                fills.append({"qty":amount,"price":stressed,"visible_price":price})
                self.consumed[(key,price)] = self.consumed.get((key,price),0)+amount
                remain = max(0, remain-amount)
            if remain < step:
                break
        return fills


def symbol_rules(info):
    fs = {x["filterType"]:x for x in info["filters"]}
    lot, price = fs["LOT_SIZE"], fs["PRICE_FILTER"]
    notional = fs.get("NOTIONAL", fs.get("MIN_NOTIONAL", {}))
    return {"step":float(lot["stepSize"]), "min_qty":float(lot["minQty"]),
            "max_qty":float(lot["maxQty"]), "tick":float(price["tickSize"]),
            "min_notional":float(notional.get("minNotional",0)),
            "max_notional":float(notional.get("maxNotional",math.inf)),
            "min_price":float(price.get("minPrice",0)),
            "max_price":float(price.get("maxPrice",0)),
            "percent_filter":fs.get("PERCENT_PRICE_BY_SIDE",fs.get("PERCENT_PRICE"))}


class Ledger:
    """Paper-only durable state. Account and settled order commit atomically."""
    def __init__(self, store, venue, initial_cash):
        self.store, self.venue, self.key = store, venue, "account:"+venue
        if store.get(self.key) is None:
            store.set(self.key, {"initial_cash":initial_cash,"cash":initial_cash,"positions":{},
                                 "fees":0,"realized_pnl":0,"peak_equity":initial_cash,
                                 "halted":False,"created_at":time.time()})
        self.recover()

    def state(self):
        return self.store.get(self.key)

    def recover(self):
        # There is no real exchange order. A pending order without atomic local
        # settlement never executed in this simulator. Do not resubmit it.
        with self.store.transaction() as db:
            rows = db.execute("SELECT id,status,payload FROM orders WHERE venue=? AND status IN ('SUBMITTED','UNKNOWN')",
                              (self.venue,)).fetchall()
            for row in rows:
                p = json.loads(row["payload"])
                status = p.get("settled_status", "CANCELED_RESTART")
                p["recovered_at"] = time.time()
                db.execute("UPDATE orders SET status=?,payload=? WHERE id=?", (status,dumps(p),row["id"]))

    def pending(self):
        return self.store.rows("SELECT * FROM orders WHERE venue=? AND status IN ('SUBMITTED','UNKNOWN')", (self.venue,))

    def submit(self, symbol, side, details, order_id=None):
        if self.pending():
            raise RuntimeError("Outstanding paper order must be reconciled before another order")
        oid = order_id or uuid.uuid4().hex
        p = {"id":oid,"venue":self.venue,"symbol":symbol,"side":side,"created":time.time(),**details}
        with self.store.transaction() as db:
            db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)", (oid,self.venue,symbol,side,"SUBMITTED",p["created"],dumps(p)))
        return p

    def finalize(self, order, status, state=None, unknown=False):
        p = {**order,"settled_status":status,"settled_at":time.time()}
        with self.store.transaction() as db:
            row = db.execute("SELECT status FROM orders WHERE id=?",(p["id"],)).fetchone()
            if not row or row[0] != "SUBMITTED":
                raise ValueError("Order is absent/already settled; duplicate settlement rejected")
            if state is not None:
                if state["cash"] < -1e-8:
                    raise ValueError("Paper account cannot borrow")
                state["cash"] = max(0,state["cash"])
                db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)",(self.key,dumps(state)))
            db.execute("UPDATE orders SET status=?,payload=? WHERE id=?",
                       ("UNKNOWN" if unknown else status,dumps(p),p["id"]))
        return p

    def reconcile(self, age=0):
        for row in self.pending():
            p = json.loads(row["payload"])
            if row["status"] == "UNKNOWN" and time.time()-p.get("settled_at",time.time()) >= age:
                with self.store.transaction() as db:
                    db.execute("UPDATE orders SET status=? WHERE id=?",(p["settled_status"],row["id"]))

    def cex_fill(self, order, fills, fee_rate, metadata=None, unknown=False):
        state = self.state()
        qty, value = sum(f["qty"] for f in fills), sum(f["qty"]*f["price"] for f in fills)
        fee = value*fee_rate
        symbol, side = order["symbol"],order["side"]
        p = state["positions"].get(symbol)
        if qty:
            if side == "BUY":
                cost = value+fee
                if cost > state["cash"]+1e-8:
                    raise ValueError("Insufficient paper cash including fees")
                p = p or {"qty":0,"cost":0,"opened_at":time.time()}
                p["qty"] += qty
                p["cost"] += cost
                p.update(metadata or {})
                state["positions"][symbol] = p
                state["cash"] -= cost
            else:
                if not p or qty>p["qty"]+1e-8:
                    raise ValueError("No paper inventory for sale")
                remaining_fraction = max(0, 1-qty/p["qty"])
                if "last_value" in p:
                    p["last_value"] *= remaining_fraction
                basis = p["cost"]*qty/p["qty"]
                p["cost"] -= basis
                p["qty"] -= qty
                state["cash"] += value-fee
                state["realized_pnl"] += value-fee-basis
                if p["qty"] < 1e-12:
                    del state["positions"][symbol]
            state["fees"] += fee
        status = "FILLED" if qty >= order["qty"]-1e-12 else ("PARTIALLY_FILLED_CANCELED" if qty else "EXPIRED")
        order.update({"fills":fills,"filled_qty":qty,"fee":fee,"fee_rate":fee_rate})
        return self.finalize(order,status,state,unknown)

    def mark(self, values, max_drawdown, extra=None):
        state = self.state()
        equity = state["cash"]+sum(values.get(k,0) for k in state["positions"])
        state["peak_equity"] = max(state["peak_equity"],equity)
        dd = 1-equity/state["peak_equity"]
        state["halted"] = state["halted"] or dd>=max_drawdown
        payload = {"cash":state["cash"],"position_values":values,"drawdown":dd,
                   "return":equity/state["initial_cash"]-1,"halted":state["halted"],**(extra or {})}
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)",(self.key,dumps(state)))
            db.execute("INSERT INTO marks(ts,venue,equity,payload) VALUES(?,?,?,?)",
                       (time.time(),self.venue,equity,dumps(payload)))
        return {"equity":equity,**payload}
