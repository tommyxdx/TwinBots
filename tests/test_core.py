import asyncio
import copy
import csv
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from twobots.config import load_config
from twobots.data import parse_klines,seconds,months_before,Fetcher
from twobots.storage import Store
from twobots.execution import Book,Ledger,quantize
from twobots.scanner import assess,Scanner
from twobots.features import price_frame,PRICE_FEATURES,regime
from twobots.models import fit_temporal,LinearProbability,scanner_training_rows
from twobots.dex import DexPaper,QuoteGateway
from twobots.demo import FixtureQuotes,scenarios
from twobots.runtime import ProcessLock
from twobots.net import HTTP
from twobots.notify import Telegram
from twobots.report import export_report

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT/"config.example.yaml")
        self.cfg["data_dir"] = self.tmp.name
        self.cfg["dex"].update(latency_ms=[0,0],dropped_probability=0,revert_probability=0,adverse_output_bps=0)
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class DataTests(Base):
    def test_microsecond_and_millisecond_timestamps(self):
        self.assertEqual(seconds(1750000000000000),1750000000)
        self.assertEqual(seconds(1750000000000),1750000000)
        self.assertEqual(seconds("2025-01-01T00:00:00Z"),1735689600)

    def test_open_candle_excluded_and_ohlc_validated(self):
        rows = [[1000000000000000,1,2,.5,1.5,10,1000000059999999,15],
                [2000000000000000,1,2,.5,1.5,10,2000000059999999,15],
                [1000000000000000,1,.8,.5,1.5,10,1000000059999999,15]]
        data = parse_klines(rows,now=1500000000)
        self.assertEqual(len(data),1)
        self.assertEqual(data[0][:2],(1000000000,1000000059))

    def test_budget_is_shared_and_bounded(self):
        self.store.budget(2)
        second = Store(self.tmp.name)
        try:
            second.budget(2)
            with self.assertRaises(RuntimeError):
                self.store.budget(2)
        finally:
            second.close()

    def test_candle_import_idempotent(self):
        data = [(1,299,1,2,.5,1,10,10)]
        self.store.add_candles("dex:solana","pool",data)
        self.store.add_candles("dex:solana","pool",data)
        self.assertEqual(len(self.store.candles("dex:solana","pool")),1)

    def test_process_lock_prevents_duplicate_engine(self):
        with ProcessLock(self.store.root,"cex"):
            with self.assertRaises(RuntimeError):
                with ProcessLock(self.store.root,"cex"):
                    pass

    def test_no_future_feature_leak(self):
        rng = np.random.default_rng(7)
        prices = 10*np.exp(np.cumsum(rng.normal(0,.01,300)))
        rows = [{"ts":i*300,"close_ts":i*300+299,"o":p,"h":p*1.01,"l":p*.99,"c":p,"v":100,"qv":1000+i}
                for i,p in enumerate(prices)]
        full = price_frame(rows)
        partial = price_frame(rows[:240])
        np.testing.assert_allclose(full.iloc[239][PRICE_FEATURES].to_numpy(float),partial.iloc[-1][PRICE_FEATURES].to_numpy(float))

    def test_gap_stops_regime(self):
        rows = [{"ts":i*300+(300 if i>=200 else 0),"close_ts":i*300+299,"o":10,"h":11,"l":9,"c":10+i*.01,"v":100,"qv":100}
                for i in range(250)]
        self.assertEqual(regime(price_frame(rows).iloc[-1]),"unknown")

    def test_plain_http_rejected(self):
        with self.assertRaises(ValueError):
            HTTP(self.cfg,self.store).request("http://example.com")


class BookTests(Base):
    def book(self):
        b=Book()
        b.snapshot({"lastUpdateId":10,"bids":[[9,2]],"asks":[[10,.2],[11,.3],[12,5]]})
        return b

    def test_sequence_gap_invalidates(self):
        b=self.book()
        self.assertFalse(b.apply({"U":12,"u":13,"b":[],"a":[]}))
        self.assertFalse(b.fresh(2000))

    def test_overlap_bridge_and_delete_level(self):
        b=self.book()
        self.assertTrue(b.apply({"U":9,"u":11,"b":[],"a":[[10,0]]}))
        self.assertEqual(b.top("BUY"),11)

    def test_stale_book(self):
        b=self.book()
        self.assertFalse(b.fresh(2000,now=b.received+3))

    def test_partial_fill_and_no_depth_reuse(self):
        b=self.book()
        f=b.ioc("BUY",1,11,1,0,.01)
        self.assertAlmostEqual(sum(x["qty"] for x in f),.5)
        self.assertEqual(b.ioc("BUY",1,11,1,0,.01),[])

    def test_adverse_fill_must_respect_limit(self):
        b=self.book()
        self.assertEqual(b.ioc("BUY",1,10,1,10,.01),[])

    def test_quantity_precision_uses_decimal(self):
        self.assertEqual(quantize(.3,.1),.3)
        self.assertEqual(quantize(.299999,.1),.2)
        self.assertEqual(quantize(1.011,.01,up=True),1.02)


class LedgerTests(Base):
    def test_fee_cash_and_unknown_recovery(self):
        l=Ledger(self.store,"cex",100)
        o=l.submit("X","BUY",{"qty":2})
        l.cex_fill(o,[{"qty":1,"price":10}],.001,unknown=True)
        self.assertAlmostEqual(l.state()["cash"],89.99)
        self.assertEqual(l.pending()[0]["status"],"UNKNOWN")
        with self.assertRaises(RuntimeError):
            l.submit("X","BUY",{"qty":1})
        recovered=Ledger(self.store,"cex",100)
        self.assertFalse(recovered.pending())
        self.assertEqual(self.store.rows("SELECT status FROM orders")[0]["status"],"PARTIALLY_FILLED_CANCELED")
        self.assertAlmostEqual(recovered.state()["cash"],89.99)

    def test_cannot_settle_twice(self):
        l=Ledger(self.store,"cex",100)
        o=l.submit("X","BUY",{"qty":1})
        l.cex_fill(o,[{"qty":1,"price":10}],.001)
        with self.assertRaises(ValueError):
            l.cex_fill(o,[{"qty":1,"price":10}],.001)
        self.assertAlmostEqual(l.state()["cash"],89.99)

    def test_no_borrow_or_short(self):
        l=Ledger(self.store,"cex",10)
        o=l.submit("X","BUY",{"qty":1})
        with self.assertRaises(ValueError):
            l.cex_fill(o,[{"qty":1,"price":10}],.001)
        self.assertEqual(l.state()["cash"],10)
        l.recover()
        o=l.submit("X","SELL",{"qty":1})
        with self.assertRaises(ValueError):
            l.cex_fill(o,[{"qty":1,"price":10}],.001)

    def test_drawdown_halt_persists(self):
        l=Ledger(self.store,"cex",100)
        o=l.submit("X","BUY",{"qty":2})
        l.cex_fill(o,[{"qty":2,"price":10}],0)
        self.assertTrue(l.mark({"X":5},.1)["halted"])
        self.assertTrue(l.mark({"X":30},.1)["halted"])

    def test_restart_cancels_unexecuted_submission(self):
        l=Ledger(self.store,"cex",100)
        l.submit("X","BUY",{"qty":1})
        l.recover()
        self.assertEqual(self.store.rows("SELECT status FROM orders")[0]["status"],"CANCELED_RESTART")
        self.assertEqual(l.state()["cash"],100)


class DexTests(Base):
    def engine(self,outputs):
        return DexPaper(self.cfg,self.store,FixtureQuotes(self.cfg["dex"]["quote_token"],outputs))

    def test_atomic_revert_charges_gas_only(self):
        d=self.engine([1000,900])
        result=asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))
        self.assertEqual(result["settled_status"],"REVERTED_GAS_CHARGED")
        self.assertEqual(d.ledger.state()["positions"],{})
        self.assertAlmostEqual(d.ledger.state()["cash"],99.99)

    def test_dropped_not_charged(self):
        self.cfg["dex"]["dropped_probability"]=1
        d=self.engine([1000])
        result=asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))
        self.assertEqual(result["settled_status"],"DROPPED_BEFORE_INCLUSION")
        self.assertEqual(d.ledger.state()["cash"],100)

    def test_buy_and_sell_virtual_raw_inventory(self):
        d=self.engine([1234,1234,2_200_000,2_200_000])
        asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))
        self.assertEqual(d.ledger.state()["positions"]["TOKEN"]["raw_qty"],1234)
        asyncio.run(d.swap("TOKEN","SELL",1234,"test"))
        self.assertFalse(d.ledger.state()["positions"])
        self.assertAlmostEqual(d.ledger.state()["cash"],100.18)
        self.assertAlmostEqual(d.ledger.state()["realized_pnl"],.18)

    def test_quote_failure_not_fabricated_fill(self):
        d=self.engine([1000])
        result=asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))
        self.assertEqual(result["settled_status"],"UNVERIFIABLE_NO_EXECUTION")
        self.assertEqual(d.ledger.state()["cash"],100)

    def test_exhausted_quote_source_does_not_strand_the_order(self):
        """StopIteration cannot be set on a Future: an escaping one would leave the
        await pending forever and block every later order on the venue."""
        d=self.engine([1000])
        asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))
        self.assertFalse(d.ledger.pending())
        d.gateway=FixtureQuotes(self.cfg["dex"]["quote_token"],[1000,1000])
        self.assertEqual(asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))["settled_status"],"FILLED")

    def test_unquotable_inventory_remains_and_marks_zero(self):
        d=self.engine([1000,1000])
        asyncio.run(d.swap("TOKEN","BUY",2_000_000,"test"))
        asyncio.run(d.mark_and_exit())
        self.assertIn("TOKEN",d.ledger.state()["positions"])
        mark=self.store.rows("SELECT equity FROM marks ORDER BY id DESC LIMIT 1")[0]
        self.assertAlmostEqual(mark["equity"],97.99)

    def test_quote_identity_rejected(self):
        self.cfg["dex"]["provider"]="generic"
        class Fake:
            def json(self,*a,**k):
                return {"input_token":"WRONG","output_token":"B","in_amount":"100","out_amount":"100", "min_out":"99","price_impact_fraction":.01,"asof":time.time()}
        with self.assertRaises(ValueError):
            QuoteGateway(self.cfg,Fake()).quote("A","B",100)


class ScannerModelTests(Base):
    def test_unknown_security_cannot_pass(self):
        pool={"created":time.time()-3600,"attributes":{"reserve_in_usd":"100000"}}
        r=assess(pool,{}, {},self.cfg["scanner"])
        self.assertFalse(r["security_verified"])
        self.assertLess(r["known"],r["total_checks"])
        self.assertIsNone(next(c["pass"] for c in r["checks"] if c["name"]=="可卖出检查"))

    def test_security_failure_blocks(self):
        r=assess({"created":time.time()-3600}, {},{"can_sell":False},self.cfg["scanner"])
        self.assertIn("can_sell",r["blocked"])

    def test_insufficient_labels_abstain(self):
        f=pd.DataFrame({"asof":[1,2],"label_known_at":[2,3],"label":[0,1],"x":[0,1]})
        m,r=fit_temporal(f,["x"])
        self.assertIsNone(m)
        self.assertEqual(r["status"],"insufficient_mature_labels")

    def test_time_split_purges_overlapping_labels_and_calibrates(self):
        rng=np.random.default_rng(13)
        n=1000
        x=rng.normal(size=n)
        f=pd.DataFrame({"asof":np.arange(n)*100+10000,"label_known_at":np.arange(n)*100+12000,
                        "label":(x+rng.normal(size=n)>.3).astype(int),"x":x})
        m,r=fit_temporal(f,["x"],300,25)
        self.assertIsNotNone(m)
        self.assertLess(r["n_train"],600)
        self.assertLess(r["n_calibration"],200)
        self.assertLess(r["brier"],r["baseline_brier"])
        m.save(Path(self.tmp.name)/"m.json")
        self.assertAlmostEqual(m.predict({"x":.3}),LinearProbability.load(Path(self.tmp.name)/"m.json").predict({"x":.3}))

    def test_same_token_cannot_leak_across_segments(self):
        f=pd.DataFrame({"asof":np.arange(1000)*100,"label_known_at":np.arange(1000)*100+10,
                        "label":np.arange(1000)%2,"x":np.arange(1000)%3,"group":"same_token"})
        m,r=fit_temporal(f,["x"],300,25,"group")
        self.assertIsNone(m)
        self.assertEqual(r["status"],"insufficient_calibration")

    def test_future_labels_cannot_train(self):
        f=pd.DataFrame({"asof":np.arange(1000),"label_known_at":time.time()+86400,
                        "label":np.arange(1000)%2,"x":1})
        m,r=fit_temporal(f,["x"])
        self.assertIsNone(m)
        self.assertEqual(r["rows"],0)


class EndToEndTests(Base):
    def test_offline_complete_demo_and_report(self):
        r=asyncio.run(scenarios(self.cfg,self.store))
        self.assertTrue(r["SYNTHETIC_ONLY"])
        self.assertEqual(r["cex_partial_filled"],.5)
        self.assertEqual(r["dex_slippage_failure"],"REVERTED_GAS_CHARGED")
        path=export_report(self.cfg,self.store)
        self.assertTrue(Path(path).exists())

    def test_telegram_deduplicates_and_disabled_does_not_queue(self):
        t=Telegram(self.cfg,self.store,None)
        t.enqueue("a","hello")
        self.assertFalse(self.store.rows("SELECT * FROM outbox"))
        self.cfg["telegram"]["enabled"]=True
        t.enqueue("a","hello")
        t.enqueue("a","hello")
        self.assertEqual(len(self.store.rows("SELECT * FROM outbox")),1)


if __name__=="__main__":
    unittest.main()
