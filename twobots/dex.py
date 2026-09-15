from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import random
import time
from decimal import Decimal, ROUND_DOWN, localcontext
from .data import number, seconds
from .execution import Ledger
from .net import auth_headers

LOG = logging.getLogger(__name__)


def stressed_raw(amount, bps):
    # Avoid binary-float rounding inventing raw units above 2**53.
    with localcontext() as ctx:
        ctx.prec = max(50, len(str(amount)) + 20)
        return int((Decimal(amount)*(Decimal(1)-Decimal(str(bps))/10000))
                   .to_integral_value(rounding=ROUND_DOWN))


class QuoteGateway:
    """Read-only quotes. No wallet, transaction signing, send or execute method."""
    def __init__(self,cfg,http):
        self.cfg,self.c,self.http = cfg,cfg["dex"],http

    def quote(self,input_token,output_token,amount):
        if not isinstance(amount,int) or amount<=0:
            raise ValueError("Quote amount must be a positive integer in raw token units")
        start = time.time()
        if self.c["provider"]=="jupiter":
            raw = self.http.json(self.c["quote_url"],
                {"inputMint":input_token,"outputMint":output_token,"amount":str(amount),
                 "slippageBps":self.c["slippage_bps"],"swapMode":"ExactIn","restrictIntermediateTokens":"true"},
                auth_headers(self.c,"quote_"))
            if not raw.get("routePlan"):
                raise ValueError("No quoted swap route")
            result = {"input_token":raw["inputMint"],"output_token":raw["outputMint"],
                      "in_amount":int(raw["inAmount"]),"out_amount":int(raw["outAmount"]),
                      "min_out":int(raw["otherAmountThreshold"]),"price_impact_fraction":abs(float(raw["priceImpactPct"])),
                      "asof":time.time(),"context_slot":raw.get("contextSlot"),
                      "timestamp_source":"local_response; slot freshness not independently verified"}
        elif self.c["provider"]=="generic":
            result = self.http.json(self.c["generic_quote_url"],
                {"network":self.cfg["scanner"]["network"],"input_token":input_token,"output_token":output_token,
                 "amount":str(amount),"slippage_bps":self.c["slippage_bps"]},auth_headers(self.c,"quote_"))
            result["asof"] = seconds(result["asof"])
            for key in ("in_amount","out_amount","min_out"):
                # Don't silently truncate fractional raw units.
                value = result[key]
                if str(value).isdigit() is False:
                    raise ValueError("Generic quote raw amounts must be integer strings or integers")
                result[key] = int(value)
        else:
            raise ValueError("Unknown quote provider")
        if result["input_token"]!=input_token or result["output_token"]!=output_token or result["in_amount"]!=amount:
            raise ValueError("Quote token/amount identity mismatch")
        impact = number(result.get("price_impact_fraction"))
        if impact is None or not 0<=impact<=self.c["max_price_impact_fraction"]:
            raise ValueError("Quote price impact unavailable or above limit")
        if not 0<result["min_out"]<=result["out_amount"]:
            raise ValueError("Invalid quote output/minimum")
        if not -5<=time.time()-result["asof"]<=self.c["quote_max_age_s"] or time.time()-start>self.c["quote_max_age_s"]:
            raise ValueError("Stale/slow quote")
        result["http_latency_s"] = time.time()-start
        return result


class DexPaper:
    venue = "dex"

    def __init__(self,cfg,store,gateway,params=None):
        self.cfg,self.c,self.store,self.gateway = cfg,params or cfg["dex"],store,gateway
        self.ledger = Ledger(store,self.venue,self.c["initial_cash"])
        self.rng = random.Random(cfg["runtime"]["random_seed"]+1)
        self.next_mark = 0

    def usd(self,raw):
        return raw/10**self.c["quote_decimals"]*self.c["quote_usd"]

    def raw_usd(self,usd):
        return int(Decimal(str(usd))/Decimal(str(self.c["quote_usd"]))*10**self.c["quote_decimals"])

    async def quote(self,a,b,amount):
        # StopIteration cannot be set on a Future, so letting one escape the worker
        # leaves this await pending forever with the order stuck in SUBMITTED, which
        # blocks every later order on the venue.
        def call():
            try:
                return self.gateway.quote(a,b,amount)
            except StopIteration as exc:
                raise RuntimeError("Quote source raised StopIteration") from exc
        return await asyncio.to_thread(call)

    async def swap(self,token,side,amount,reason,prepared=None):
        stable = self.c["quote_token"]
        a,b = (stable,token) if side=="BUY" else (token,stable)
        q = prepared or await self.quote(a,b,amount)
        if time.time()-q["asof"]>self.c["quote_max_age_s"]:
            return {"status":"QUOTE_EXPIRED"}
        gas = self.c["gas_usd_per_tx"]+self.c["extra_fee_usd"]
        state = self.ledger.state()
        if state["cash"]<gas+(self.usd(amount) if side=="BUY" else 0):
            return {"status":"INSUFFICIENT_CASH_FOR_INPUT_AND_GAS"}
        if side=="SELL" and amount>state["positions"].get(token,{}).get("raw_qty",0):
            raise ValueError("DEX sale exceeds virtual token balance")
        delay = self.rng.uniform(*self.c["latency_ms"])/1000
        order = self.ledger.submit(token,side,{"raw_input":amount,"initial_quote":q,"latency_s":delay,
                                               "reason":reason,"atomic_swap":True})
        await asyncio.sleep(delay)
        if self.rng.random()<self.c["dropped_probability"]:
            order["network_fee"] = 0
            return self.ledger.finalize(order,"DROPPED_BEFORE_INCLUSION")
        try:
            arrival = await self.quote(a,b,amount)
        except Exception:
            # Quote failure is NOT evidence of an on-chain failure; no fictitious gas.
            return self.ledger.finalize(order,"UNVERIFIABLE_NO_EXECUTION")
        output = stressed_raw(arrival["out_amount"], self.c["adverse_output_bps"])
        order.update({"arrival_quote":arrival,"stressed_output":output,"network_fee":gas})
        state = self.ledger.state()
        state["cash"] -= gas
        state["fees"] += gas
        if output<q["min_out"] or self.rng.random()<self.c["revert_probability"]:
            state["realized_pnl"] -= gas
            return self.ledger.finalize(order,"REVERTED_GAS_CHARGED",state)
        if side=="BUY":
            cost = self.usd(amount)
            state["cash"] -= cost
            p = state["positions"].get(token) or {"raw_qty":0,"cost":0,"opened_at":time.time(),
                                                  "high_value":0,"last_mark_at":0,"last_value":0}
            p["raw_qty"] += output
            p["cost"] += cost+gas
            state["positions"][token] = p
        else:
            p = state["positions"][token]
            basis = p["cost"]*amount/p["raw_qty"]
            proceeds = self.usd(output)
            state["cash"] += proceeds
            state["realized_pnl"] += proceeds-gas-basis
            p["cost"] -= basis
            p["raw_qty"] -= amount
            if p["raw_qty"]==0:
                del state["positions"][token]
        result = self.ledger.finalize(order,"FILLED",state)
        LOG.info("PAPER %s %s %s: FILLED",self.venue.upper(),side,token)
        return result

    async def entry(self,scan):
        token = scan["token"]
        amount = self.raw_usd(self.c["ticket_usd"])
        buy = await self.quote(self.c["quote_token"],token,amount)
        available = stressed_raw(buy["out_amount"], self.c["adverse_output_bps"])
        if available < buy["min_out"]:
            return {"status": "ROUNDTRIP_COST_REJECTED", "reason": "buy_stress_below_minimum"}
        sell = await self.quote(token,self.c["quote_token"],available)
        total_fee = 2*(self.c["gas_usd_per_tx"]+self.c["extra_fee_usd"])
        proceeds = self.usd(stressed_raw(sell["out_amount"], self.c["adverse_output_bps"]))
        roundtrip_cost = (self.usd(amount)-proceeds+total_fee)/self.usd(amount)
        # The buy/sell quotes are sequential, so this is a cost screen, not arbitrage.
        if roundtrip_cost<0 or roundtrip_cost>self.c["max_roundtrip_cost_fraction"]:
            return {"status":"ROUNDTRIP_COST_REJECTED","cost_fraction":roundtrip_cost}
        if (not 0 <= time.time()-(scan.get("history_asof") or 0) <= 900 or
                (self.c["require_security_checks"] and not -5 <=
                 time.time()-scan.get("security_source",{}).get("asof",0) <= self.cfg["scanner"]["feature_max_age_s"])):
            return {"status": "ENTRY_DATA_EXPIRED"}
        return await self.swap(token,"BUY",amount,"scanner_and_trend_gate",buy)

    async def mark_and_exit(self):
        for token,p in list(self.ledger.state()["positions"].items()):
            try:
                q = await self.quote(token,self.c["quote_token"],int(p["raw_qty"]))
                value = max(0,self.usd(stressed_raw(q["out_amount"],self.c["adverse_output_bps"]))
                            -self.c["gas_usd_per_tx"]-self.c["extra_fee_usd"])
                p.update({"last_value":value,"last_mark_at":time.time(),"high_value":max(p["high_value"],value)})
                state = self.ledger.state()
                state["positions"][token] = p
                self.store.set(self.ledger.key,state)
                stop = value<=p["cost"]*(1-self.c["stop_fraction"])
                trailing = (p["high_value"]>=p["cost"]*self.c["take_profit_activate_multiple"]
                            and value<=p["high_value"]*(1-self.c["trail_fraction"]))
                expired = time.time()-p["opened_at"]>self.c["max_hold_hours"]*3600
                if stop or trailing or expired:
                    await self.swap(token,"SELL",int(p["raw_qty"]),"stop" if stop else ("trailing" if trailing else "time_exit"),q)
            except Exception as exc:
                self.store.event(self.venue+"_mark_unavailable",{"token":token,"type":type(exc).__name__})
        state = self.ledger.state()
        values,stale = {},[]
        for token,p in state["positions"].items():
            if not p["last_mark_at"] or time.time()-p["last_mark_at"]>self.c["stale_mark_s"]:
                values[token] = 0
                stale.append(token)
            else:
                values[token] = p["last_value"]
        self.ledger.mark(values,self.c["max_drawdown"],{"unquotable_marked_zero":stale,
            "valuation":"stressed reverse quote less configured gas; inventory retained when unquotable",
            "quote_asset_assumption":self.c["quote_asset_assumption"]})

    async def step(self):
        self.store.set("heartbeat:dex",time.time())
        if time.time()>=self.next_mark:
            await self.mark_and_exit()
            self.next_mark = time.time()+self.c["mark_every_s"]
        state = self.ledger.state()
        if state["halted"] or len(state["positions"])>=self.c["max_positions"]:
            return
        # A newer failed/low-score observation must supersede an older safe one,
        # including observations of another pool for the same token.
        rows = self.store.rows("""SELECT s.* FROM scans s WHERE s.network=? AND s.ts>?
            AND NOT EXISTS (SELECT 1 FROM scans newer WHERE newer.network=s.network
                AND newer.token=s.token AND (newer.ts>s.ts OR (newer.ts=s.ts AND newer.id>s.id)))
            ORDER BY s.ts DESC LIMIT 20""",
            (self.cfg["scanner"]["network"],time.time()-2*self.cfg["scanner"]["poll_s"]))
        for row in rows:
            scan = json.loads(row["payload"])
            token = scan["token"]
            if (token in state["positions"] or scan["blocked"] or not scan["history_fresh"]
                    or scan["score"] < self.c["min_score"] or not scan.get("market_eligible", False)
                    or not 0 <= time.time()-(scan.get("history_asof") or 0) <= 900):
                continue
            if self.c["require_security_checks"] and not scan.get("risk_verified", False):
                continue
            if (self.c["require_security_checks"] and
                    not -5 <= time.time()-scan.get("security_source",{}).get("asof",0)<=self.cfg["scanner"]["feature_max_age_s"]):
                continue
            r = scan["features"]
            # Adaptive sizing/entry filter: overheated/falling/noisy conditions stay in cash.
            if not (0<r.get("ret6",-1)<.3 and 0<r.get("ret1",-1)<.12 and
                    0<r.get("atr_pct",1)<.12 and r.get("volume_ratio",0)>1.2):
                continue
            key = "dex:attempt:"+token
            if time.time()-self.store.get(key,0)<3600:
                continue
            self.store.set(key,time.time())
            result = await self.entry(scan)
            self.store.event("dex_entry_result",{"token":token,**result})
            # One candidate per iteration; cash/inventory are re-read next time.
            break
        self.store.set("heartbeat:dex",time.time())

    async def liquidate(self,reason="shutdown"):
        """Sell every position at the current quote. Only on explicit request.

        Forcing exits the strategy did not call for adds a round trip's cost and
        noise to the record, so this is never part of an ordinary restart.
        """
        results = []
        for token,p in list(self.ledger.state()["positions"].items()):
            try:
                result = await self.swap(token,"SELL",int(p["raw_qty"]),reason)
                results.append({"token":token,"status":result.get("settled_status",result.get("status"))})
            except Exception as exc:
                self.store.event(self.venue+"_liquidate_failed",{"token":token,"type":type(exc).__name__})
                results.append({"token":token,"status":"FAILED"})
        return results

    async def run(self):
        while True:
            try:
                await self.step()
            except Exception as exc:
                LOG.warning("%s paper loop error: %s",self.venue.upper(),exc)
                self.store.event(self.venue+"_error",{"type":type(exc).__name__})
                self.ledger.recover()
            await asyncio.sleep(self.c["poll_s"])
