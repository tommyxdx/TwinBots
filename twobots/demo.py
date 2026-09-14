from __future__ import annotations
import asyncio
import copy
import json
import time
from .dex import DexPaper
from .execution import Book,Ledger
from .scanner import assess
from .storage import Store
from .report import export_report


class FixtureQuotes:
    """Synthetic quote sequence for tests/demo ONLY."""
    def __init__(self,stable,outputs):
        self.stable,self.outputs = stable,iter(outputs)

    def quote(self,a,b,amount):
        out = next(self.outputs)
        return {"input_token":a,"output_token":b,"in_amount":amount,"out_amount":out,
                "min_out":int(out*.99),"price_impact_fraction":.001,"asof":time.time(),"source":"SYNTHETIC_FIXTURE"}


async def scenarios(cfg,store):
    c = copy.deepcopy(cfg)
    c["dex"].update({"latency_ms":[0,0],"dropped_probability":0,"revert_probability":0,"adverse_output_bps":0})
    ledger = Ledger(store,"cex",100)
    book = Book()
    book.snapshot({"lastUpdateId":1,"bids":[[9.9,1]],"asks":[[10,.2],[10.1,.3],[10.2,100]]})
    order = ledger.submit("DEMOUSDT","BUY",{"qty":1,"limit":10.1})
    fills = book.ioc("BUY",1,10.1,1,0,.01)
    result = ledger.cex_fill(order,fills,.001,unknown=True)
    unknown_status = ledger.pending()[0]["status"]
    ledger.reconcile()
    mark = ledger.mark({"DEMOUSDT":.5*9.9*.999},.1)
    dex = DexPaper(c,store,FixtureQuotes(c["dex"]["quote_token"],[1000,1000,2_200_000,2_200_000,1000,500]))
    buy = await dex.swap("SYNTHETIC","BUY",2_000_000,"demo")
    sell = await dex.swap("SYNTHETIC","SELL",1000,"demo")
    reverted = await dex.swap("SYNTHETIC2","BUY",2_000_000,"demo_slippage_failure")
    dex.ledger.mark({},.1)
    pool = {"created":time.time()-3600,"attributes":{"reserve_in_usd":"100000",
            "volume_usd":{"h1":"50000"},"transactions":{"h1":{"buys":50,"sells":30}}}}
    score = assess(pool,{"ret6":.1,"volume_ratio":2,"drawdown42":-.1},{},c["scanner"])
    report = {"SYNTHETIC_ONLY":True,"meaning":"Software behavior demo. These numbers are NOT historical or live returns.",
              "cex_partial_filled":result["filled_qty"],"cex_unknown_ack":unknown_status,
              "cex_equity":mark["equity"],"dex_buy":buy["settled_status"],"dex_sell":sell["settled_status"],
              "dex_slippage_failure":reverted["settled_status"],"scanner_missing_security":score}
    store.set("demo",report)
    return report


def run_demo(cfg,root):
    from pathlib import Path
    root = Path(root)
    # Preserve a prior demo instead of accidentally overwriting an account.
    if (root/"state.sqlite3").exists():
        root = root/str(time.time_ns())
    store = Store(root)
    try:
        result = asyncio.run(scenarios(cfg,store))
        (root/"SYNTHETIC_DEMO.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        export_report(cfg,store)
        return {"directory":str(root.resolve()),**result}
    finally:
        store.close()
