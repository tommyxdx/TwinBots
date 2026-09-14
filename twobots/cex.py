from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import random
import time
from urllib.parse import urlencode
import websockets
from .data import INTERVALS
from .execution import Book, Ledger, quantize, symbol_rules
from .features import price_frame, regime, signal
from .models import LinearProbability, model_context, model_unavailable

LOG = logging.getLogger(__name__)


class CexPaper:
    def __init__(self, cfg, store, http, fetcher):
        self.cfg,self.c,self.store,self.http,self.fetcher = cfg,cfg["cex"],store,http,fetcher
        self.ledger = Ledger(store,"cex",self.c["initial_cash"])
        self.books,self.rules,self.fees = {},{},{}
        self.rng = random.Random(cfg["runtime"]["random_seed"])
        self.last_mark = 0

    def metadata(self):
        data = self.http.json(self.c["rest_base"]+"/api/v3/exchangeInfo",
                              {"symbols":json.dumps(self.c["symbols"],separators=(",",":"))})
        for info in data["symbols"]:
            if info["status"] != "TRADING" or info["quoteAsset"] != "USDT" or not info.get("isSpotTradingAllowed",True):
                LOG.warning("Unsupported/inactive spot symbol: %s",info["symbol"])
                continue
            self.rules[info["symbol"]] = symbol_rules(info)
        if not self.rules:
            raise ValueError("No active USDT spot symbols available")
        self.store.set("cex:filters_at",time.time())
        if self.c["fetch_account_fees"]:
            self.account_fees()

    def account_fees(self):
        trusted = {"https://api.binance.com", *(f"https://api{i}.binance.com" for i in range(1,5))}
        if self.c["rest_base"].rstrip("/") not in trusted:
            raise ValueError("Signed fee queries require an official Binance REST origin")
        key,secret = os.getenv("BINANCE_API_KEY"),os.getenv("BINANCE_API_SECRET")
        if not key or not secret:
            raise ValueError("Fee lookup enabled but local Binance credentials are missing")
        for symbol in self.rules:
            p = {"symbol":symbol,"timestamp":int(time.time()*1000),"recvWindow":5000}
            p["signature"] = hmac.new(secret.encode(),urlencode(p).encode(),hashlib.sha256).hexdigest()
            raw = self.http.json(self.c["rest_base"]+"/api/v3/account/commission",p,{"X-MBX-APIKEY":key})
            if raw.get("symbol") != symbol or "standardCommission" not in raw:
                raise ValueError("Commission response is missing symbol or standard fees")
            rates = {}
            for side,leg in (("BUY","buyer"),("SELL","seller")):
                components = [float(raw[k][field]) for k in
                              ("standardCommission", "taxCommission", "specialCommission")
                              if k in raw for field in ("taker", leg)]
                if not all(math.isfinite(x) and 0 <= x < 1 for x in components) or sum(components) >= 1:
                    raise ValueError("Invalid commission rate")
                rates[side] = sum(components)
            # No BNB balance assumption; model undiscounted fee in quote currency.
            self.fees[symbol] = rates
        self.store.set("cex:fee_rates",{"at":time.time(),"rates":self.fees,"BNB_discount_applied":False})

    async def feed(self,symbol):
        url = self.c["ws_base"].rstrip("/")+"/ws/"+symbol.lower()+"@depth@100ms"
        backoff = 5
        while True:
            try:
                async with websockets.connect(url,ping_interval=20,ping_timeout=20,max_queue=4096) as ws:
                    # Open stream before snapshot. websockets buffers intervening diffs.
                    book = Book()
                    snap = await asyncio.to_thread(self.http.json,self.c["rest_base"]+"/api/v3/depth",
                                                   {"symbol":symbol,"limit":self.c["snapshot_limit"]})
                    book.snapshot(snap)
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("e") != "depthUpdate":
                            continue
                        if int(event["u"]) <= book.update_id:
                            continue
                        received = min(time.time(),float(event.get("E",time.time()*1000))/1000)
                        if not book.apply(event,received):
                            raise ValueError("Depth sequence gap/crossed book; rebuilding")
                        # Publish only after the first bridging diff, never an unsynced snapshot.
                        self.books[symbol] = book
                        backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if symbol in self.books:
                    self.books[symbol].valid = False
                LOG.warning("Depth %s unavailable (%s); no fills until synchronized",symbol,type(exc).__name__)
                self.store.event("cex_feed_unavailable",{"symbol":symbol,"type":type(exc).__name__})
                await asyncio.sleep(backoff)
                backoff = min(60,backoff*2)

    async def order(self,symbol,side,quantity,reason,metadata=None):
        def rejected(status,**details):
            result = {"status":status,"symbol":symbol,"side":side,"reason":reason,**details}
            self.store.event("cex_rejection",result)
            return result
        book,rule = self.books.get(symbol),self.rules[symbol]
        if not book or not book.fresh(self.c["depth_stale_ms"]):
            return rejected("NO_FRESH_BOOK")
        fee = self.fees.get(symbol,{}).get(side,self.c["fee_rate"])
        reference = book.top(side)
        limit = quantize(reference*(1+self.c["slippage_limit_bps"]/10000*(1 if side=="BUY" else -1)),
                         rule["tick"],up=side=="SELL")
        state = self.ledger.state()
        if side == "BUY":
            quantity = min(quantity,state["cash"]/(limit*(1+fee)))
        else:
            quantity = min(quantity,state["positions"].get(symbol,{}).get("qty",0))
        qty = quantize(min(quantity,rule["max_qty"]),rule["step"])
        if (qty<rule["min_qty"] or qty*limit<rule["min_notional"] or qty*limit>rule["max_notional"]
                or limit<rule["min_price"] or (rule["max_price"] and limit>rule["max_price"])):
            return rejected("FILTER_REJECTED",qty=qty)
        # Dynamic percent-price filters use venue weighted-average price.
        percent = rule.get("percent_filter")
        if percent:
            avg = await asyncio.to_thread(self.http.json,self.c["rest_base"]+"/api/v3/avgPrice",{"symbol":symbol})
            if int(avg.get("mins",-1)) != int(percent.get("avgPriceMins",0)):
                return rejected("PERCENT_FILTER_UNVERIFIABLE")
            base = float(avg["price"])
            prefix = "bid" if side=="BUY" else "ask"
            low = float(percent.get(prefix+"MultiplierDown",percent.get("multiplierDown",0)))
            high = float(percent.get(prefix+"MultiplierUp",percent.get("multiplierUp",0)))
            if not base*low<=limit<=base*high:
                return rejected("PERCENT_FILTER_REJECTED")
        latency = self.rng.uniform(*self.c["latency_ms"])/1000
        order = self.ledger.submit(symbol,side,{"qty":qty,"limit":limit,"reason":reason,
                                               "latency_s":latency,"type":"LIMIT_IOC"})
        await asyncio.sleep(latency)
        arrival = self.books.get(symbol)
        if not arrival or not arrival.fresh(self.c["depth_stale_ms"]):
            return self.ledger.finalize(order,"EXPIRED_STALE_BOOK")
        fills = arrival.ioc(side,qty,limit,self.c["visible_liquidity_fraction"],self.c["adverse_fill_bps"],rule["step"])
        order["arrival_book_update_id"] = arrival.update_id
        result = self.ledger.cex_fill(order,fills,fee,metadata,
                                      unknown=self.rng.random()<self.c["lost_ack_probability"])
        LOG.info("PAPER CEX %s %s: %s filled=%s",side,symbol,result["settled_status"],result["filled_qty"])
        return result

    def gate(self,row):
        if self.c["strategy"] == "rules":
            return True,{"status":"rules_only"}
        path = self.store.root/"models"/"cex_gate.json"
        if not path.exists():
            return False,{"status":"missing_model"}
        try:
            m = LinearProbability.load(path)
        except (OSError, ValueError):
            return False,{"status":"invalid_model"}
        metrics = m.data["metrics"]
        unavailable = model_unavailable(m.data, self.c["model_refresh_days"]*86400, model_context(self.cfg, "cex"))
        if unavailable:
            return False,{"status":unavailable}
        if metrics["brier"]>=metrics["baseline_brier"] or metrics["average_precision"]<=metrics["test_prevalence"]:
            return False,{"status":"no_holdout_advantage"}
        p = m.predict(row)
        return p is not None and p>=self.c["model_threshold"],{"status":"research_gate","p":p}

    async def step(self):
        self.store.set("heartbeat:cex",time.time())
        self.ledger.reconcile(self.c["ack_reconcile_after_s"])
        if self.ledger.pending():
            return
        state = self.ledger.state()
        values,stale = {},[]
        for symbol,p in list(state["positions"].items()):
            book = self.books.get(symbol)
            if not book or not book.fresh(self.c["depth_stale_ms"]):
                # Hold last mark temporarily; it is explicitly not executable.
                values[symbol] = p.get("last_value",p["cost"])
                stale.append(symbol)
                continue
            bid = book.top("SELL")
            p["high_water"] = max(p.get("high_water",bid),bid)
            p["stop"] = max(p.get("stop",0),p["high_water"]-self.c["trail_atr"]*p.get("atr",bid*.02))
            p["last_value"] = p["qty"]*bid*(1-self.fees.get(symbol,{}).get("SELL",self.c["fee_rate"]))
            values[symbol] = p["last_value"]
        # Save mark/trailing metadata before an order changes inventory.
        self.store.set(self.ledger.key,state)
        if time.time()-self.last_mark>=self.cfg["runtime"]["heartbeat_s"]:
            mark = self.ledger.mark(values,self.c["max_drawdown"],{"stale_symbols":stale,
                "valuation":"best bid less fee; exit depth/impact applied only on execution"})
            self.last_mark = time.time()
            LOG.info("CEX paper equity %.4f, halted=%s",mark["equity"],mark["halted"])
        for symbol,p in list(self.ledger.state()["positions"].items()):
            book = self.books.get(symbol)
            if (book and book.fresh(self.c["depth_stale_ms"]) and (book.top("SELL")<=p.get("stop",0) or p.get("exit_reason"))
                    and time.time()-self.store.get("cex:exit_attempt:"+symbol,0)>30):
                self.store.set("cex:exit_attempt:"+symbol,time.time())
                state = self.ledger.state()
                state["positions"][symbol].setdefault("exit_reason", "stop_or_trailing")
                self.store.set(self.ledger.key, state)
                await self.order(symbol,"SELL",p["qty"],p.get("exit_reason","stop_or_trailing"))
                if self.ledger.pending():
                    return
        period = int(time.time()//INTERVALS[self.c["interval"]])
        for symbol in self.rules:
            if self.store.get("cex:evaluated:"+symbol)==period:
                continue
            # A new closed 4h bar, not a REST poll every loop.
            await asyncio.to_thread(self.fetcher.recent_cex,symbol,300)
            f = price_frame(self.store.candles("cex",symbol,500),INTERVALS[self.c["interval"]])
            if f.empty or time.time()-f.iloc[-1]["close_ts"]>INTERVALS[self.c["interval"]]+60:
                continue
            row = f.iloc[-1]
            market = regime(row)
            action = signal(row,market)
            allowed,gate = self.gate(row.to_dict())
            self.store.event("cex_signal",{"symbol":symbol,"bar":int(row["ts"]),"regime":market,"signal":action,"gate":gate})
            state = self.ledger.state()
            if symbol in state["positions"] and market in ("range","stress"):
                state["positions"][symbol]["exit_reason"] = "regime_exit"
                self.store.set(self.ledger.key,state)
                self.store.set("cex:exit_attempt:"+symbol,time.time())
                await self.order(symbol,"SELL",state["positions"][symbol]["qty"],"regime_exit")
            elif (action and allowed and not state["halted"] and not stale and symbol not in state["positions"]
                  and len(state["positions"])<self.c["max_positions"]):
                book = self.books.get(symbol)
                if not book or not book.fresh(self.c["depth_stale_ms"]):
                    continue  # Retry evaluation once a synchronized book exists.
                price = book.top("BUY")
                # Orders earlier in this loop may have partially sold inventory.
                equity = state["cash"]+sum(p.get("last_value",p["cost"]) for p in state["positions"].values())
                distance = min(self.c["max_stop_fraction"],max(self.c["min_stop_fraction"],
                               self.c["stop_atr"]*float(row["atr"])/price))
                # Cost allowance means risk sizing includes estimated round-trip fees.
                buy_fee = self.fees.get(symbol,{}).get("BUY",self.c["fee_rate"])
                sell_fee = self.fees.get(symbol,{}).get("SELL",self.c["fee_rate"])
                cost = buy_fee + sell_fee + 2*self.c["slippage_limit_bps"]/10000
                budget = min(equity*self.c["max_position_fraction"],equity*self.c["risk_fraction"]/(distance+cost))
                worst_price = price*(1+self.c["slippage_limit_bps"]/10000)*(1+buy_fee)
                await self.order(symbol,"BUY",budget/worst_price,action,
                                 {"stop":price*(1-distance),"atr":float(row["atr"]),"high_water":price,"regime":market})
            self.store.set("cex:evaluated:"+symbol,period)
            if self.ledger.pending():
                break
        self.store.set("heartbeat:cex",time.time())

    async def run(self):
        # Retry unavailable metadata without pretending the bot is connected.
        while True:
            try:
                await asyncio.to_thread(self.metadata)
                break
            except Exception as exc:
                LOG.warning("CEX metadata unavailable: %s",exc)
                await asyncio.sleep(30)
        feeds = [asyncio.create_task(self.feed(s)) for s in self.rules]
        try:
            while True:
                try:
                    if time.time()-self.store.get("cex:filters_at",0)>86400:
                        await asyncio.to_thread(self.metadata)
                    await self.step()
                except Exception as exc:
                    LOG.exception("CEX paper loop error (%s)",type(exc).__name__)
                    self.store.event("cex_error",{"type":type(exc).__name__})
                    # A paper-only order interrupted before settlement is canceled;
                    # atomically settled unknown acknowledgments are reconciled.
                    self.ledger.recover()
                    await asyncio.sleep(30)
                await asyncio.sleep(2)
        finally:
            for t in feeds:
                t.cancel()
            await asyncio.gather(*feeds,return_exceptions=True)
