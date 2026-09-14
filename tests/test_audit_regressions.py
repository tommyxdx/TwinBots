"""Offline regressions for the 1.1.0 audit; no profitability claims."""
import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import numpy as np
import pandas as pd
import yaml
from twobots.cex import CexPaper
from twobots.config import load_config
from twobots.data import Fetcher
from twobots.dex import DexPaper, stressed_raw
from twobots.execution import Book, Ledger
from twobots.models import fit_temporal, model_context, model_unavailable
from twobots.net import HTTP
from twobots.report import build_report
from twobots.runtime import maintain, maintenance_loop
from twobots.scanner import Scanner, assess
from twobots.storage import Store, dumps

ROOT = Path(__file__).resolve().parents[1]


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.example.yaml")
        self.cfg["data_dir"] = self.tmp.name
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def scan(self):
        return {"network": "solana", "token": "T", "pool": "P", "at": time.time(),
                "score": 95, "blocked": [], "history_fresh": True, "history_asof": time.time()-1,
                "market_eligible": True, "security_verified": True, "risk_verified": True,
                "security_source": {"asof": time.time()},
                "features": {"ret6": .1, "ret1": .02, "atr_pct": .04, "volume_ratio": 2}}

    def save_scan(self, scan):
        with self.store.transaction() as db:
            db.execute("INSERT INTO scans(ts,network,token,pool,score,payload) VALUES(?,?,?,?,?,?)",
                       (scan["at"], scan["network"], scan["token"], scan["pool"], scan["score"], dumps(scan)))

    def dex(self):
        d = DexPaper(self.cfg, self.store, None)
        d.next_mark = time.time()+100
        d.entry = AsyncMock(return_value={"status": "fixture"})
        return d

    def test_newer_unsafe_scan_supersedes_old_high_score(self):
        old = self.scan()
        self.save_scan(old)
        latest = copy.deepcopy(old)
        latest.update(pool="P2", score=5, blocked=["can_sell"])
        self.save_scan(latest)  # Same timestamp; id breaks ties.
        d = self.dex()
        asyncio.run(d.step())
        d.entry.assert_not_awaited()

    def test_newer_safe_scan_can_enter(self):
        old = self.scan()
        old.update(score=5, blocked=["can_sell"])
        self.save_scan(old)
        self.save_scan(self.scan())
        d = self.dex()
        asyncio.run(d.step())
        d.entry.assert_awaited_once()

    def test_entry_requires_risk_market_and_fresh_history(self):
        for change in ({"risk_verified": False}, {"market_eligible": False},
                       {"history_asof": time.time()-901},
                       {"security_source": {"asof": time.time()+60}}):
            with self.subTest(change=change):
                s = self.scan()
                s.update(change)
                self.save_scan(s)
                d = self.dex()
                asyncio.run(d.step())
                d.entry.assert_not_awaited()

    def test_concentration_failure_cannot_be_offset_by_score(self):
        sec = {"can_sell": True, "mint_revoked": True, "freeze_revoked": True,
               "top10_ex_lp_fraction": .9, "largest_funding_cluster_fraction": .1, "creator_bad_rate": .1}
        r = assess({"created": time.time()-3600}, {}, sec, self.cfg["scanner"])
        self.assertIn("top10_ex_lp_fraction", r["blocked"])
        self.assertFalse(r["risk_verified"])

    def test_missing_concentration_remains_unverified(self):
        r = assess({"created": time.time()-3600}, {},
                   {"can_sell": True, "mint_revoked": True, "freeze_revoked": True}, self.cfg["scanner"])
        self.assertTrue(r["security_verified"])
        self.assertFalse(r["risk_verified"])

    def test_invalid_security_fraction_rejected(self):
        self.cfg["scanner"]["feature_url_template"] = "https://fixture.test/{token}"
        pool = {"network": "solana", "token": "T", "pool": "P"}
        for value in (True, float("nan"), -1, 100):
            http = Mock()
            http.json.return_value = {**pool, "asof": time.time(), "features": {"creator_bad_rate": value}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                Scanner(self.cfg, self.store, http, None, None).security(pool)

    def test_partial_sale_scales_stale_mark(self):
        l = Ledger(self.store, "cex", 100)
        l.cex_fill(l.submit("T", "BUY", {"qty": 2}), [{"qty": 2, "price": 10}], 0, {"last_value": 20})
        l.cex_fill(l.submit("T", "SELL", {"qty": 2}), [{"qty": 1, "price": 10}], 0)
        p = l.state()["positions"]["T"]
        self.assertEqual(p["last_value"], 10)
        self.assertEqual(l.state()["cash"]+p["last_value"], 100)

    def test_partial_stop_remains_an_exit_intent(self):
        c = CexPaper(self.cfg, self.store, None, None)
        c.ledger.cex_fill(c.ledger.submit("T", "BUY", {"qty": 2}), [{"qty": 2, "price": 10}], 0,
                          {"stop": 9.5, "atr": 1, "high_water": 10})
        b = Book()
        b.snapshot({"lastUpdateId": 1, "bids": [[9, 1]], "asks": [[10, 1]]})
        c.books["T"] = b
        async def partial(*args):
            c.ledger.cex_fill(c.ledger.submit("T", "SELL", {"qty": 2}), [{"qty": 1, "price": 9}], 0)
        c.order = partial
        asyncio.run(c.step())
        self.assertEqual(c.ledger.state()["positions"]["T"]["exit_reason"], "stop_or_trailing")

    def test_raw_inventory_above_float_precision_is_exact(self):
        for amount in (2**53+1, 2**64-1, 10**24+123):
            self.assertEqual(stressed_raw(amount, 0), amount)
            self.assertEqual(stressed_raw(amount, 30), amount*997//1000)

    def test_roundtrip_screen_includes_output_stress(self):
        d = DexPaper(self.cfg, self.store, None)
        d.quote = AsyncMock(side_effect=[
            {"out_amount": 1000, "min_out": 990, "asof": time.time()},
            {"out_amount": 1965000, "asof": time.time()}])
        d.swap = AsyncMock()
        result = asyncio.run(d.entry(self.scan()))
        self.assertEqual(d.quote.await_args_list[1].args[2], 997)
        self.assertEqual(result["status"], "ROUNDTRIP_COST_REJECTED")
        d.swap.assert_not_awaited()

    def test_cex_position_budget_includes_fees_and_limit_price(self):
        self.cfg["cex"].update(strategy="rules", risk_fraction=.1, max_position_fraction=.25)
        c = CexPaper(self.cfg, self.store, None, Mock())
        c.rules = {"T": {}}
        c.fees = {"T": {"BUY": .01, "SELL": .02}}
        b = Book()
        b.snapshot({"lastUpdateId": 1, "bids": [[9.99, 100]], "asks": [[10, 100]]})
        c.books["T"] = b
        c.order = AsyncMock()
        f = pd.DataFrame([{"close_ts": time.time()-1, "ts": 1, "atr": .2}])
        with patch("twobots.cex.price_frame", return_value=f), patch("twobots.cex.regime", return_value="trend"), \
                patch("twobots.cex.signal", return_value="breakout"):
            asyncio.run(c.step())
        qty = c.order.await_args.args[2]
        worst_cost = qty*10*(1+self.cfg["cex"]["slippage_limit_bps"]/10000)*1.01
        self.assertAlmostEqual(worst_cost, 25)

    def test_retraining_cannot_freshen_old_history(self):
        now = time.time()
        context = model_context(self.cfg, "cex")
        d = {"created_at": now, "context": context, "metrics": {"label_max_time": now-8*86400}}
        self.assertEqual(model_unavailable(d, 7*86400, context, now), "stale_history")
        d["metrics"]["label_max_time"] = now-1
        self.assertIsNone(model_unavailable(d, 7*86400, context, now))
        changed = {**context, "fee_rate": .003}
        self.assertEqual(model_unavailable(d, 7*86400, changed, now), "model_configuration_changed")

    def test_cex_gate_refuses_old_data_even_with_favorable_metrics(self):
        c = CexPaper(self.cfg, self.store, None, None)
        folder = self.store.root / "models"
        folder.mkdir()
        (folder / "cex_gate.json").write_text(json.dumps({"created_at": time.time(),
            "context": model_context(self.cfg, "cex"), "metrics": {"label_max_time": time.time()-10*86400,
            "brier": .1, "baseline_brier": .2, "average_precision": .8, "test_prevalence": .5}}))
        self.assertEqual(c.gate({}), (False, {"status": "stale_history"}))

    def test_optional_feature_selection_only_uses_training(self):
        rng = np.random.default_rng(13)
        x = rng.normal(size=1000)
        f = pd.DataFrame({"asof": np.arange(1000)*100+10000, "label_known_at": np.arange(1000)*100+10001,
                          "label": (x+rng.normal(size=1000)>0).astype(int), "x": x,
                          "extra": np.where(np.arange(1000)<600, x, np.nan)})
        model, report = fit_temporal(f, ["x"], optional_features=["extra"])
        self.assertIn("extra", model.data["features"])
        f.loc[600:, "extra"] = 9999
        second, _ = fit_temporal(f, ["x"], optional_features=["extra"])
        self.assertEqual(model.data["features"], second.data["features"])
        self.assertEqual(model.data["coef"], second.data["coef"])
        self.assertLess(report["train_label_through"], report["calibration_from"])
        self.assertLess(report["calibration_label_through"], report["test_from"])

    def test_signed_fee_query_rejects_custom_host_before_request(self):
        self.cfg["cex"]["rest_base"] = "https://fixture.test"
        http = Mock()
        c = CexPaper(self.cfg, self.store, http, None)
        with self.assertRaises(ValueError):
            c.account_fees()
        http.json.assert_not_called()

    def test_malformed_fee_response_never_becomes_zero_cost(self):
        http = Mock()
        c = CexPaper(self.cfg, self.store, http, None)
        c.rules = {"BTCUSDT": {}}
        http.json.return_value = {"symbol": "BTCUSDT"}
        with patch.dict("os.environ", {"BINANCE_API_KEY": "fixture", "BINANCE_API_SECRET": "fixture"}):
            with self.assertRaises(ValueError):
                c.account_fees()
        self.assertFalse(c.fees)

    def test_maintenance_keeps_old_equity_evidence(self):
        l = Ledger(self.store, "cex", 100)
        l.mark({}, .1)
        with self.store.transaction() as db:
            db.execute("UPDATE marks SET ts=?", (time.time()-90*86400,))
        self.store.set("models:last_attempt", time.time())
        maintain(self.cfg, self.store, Mock())
        self.assertEqual(len(self.store.rows("SELECT * FROM marks")), 1)
        report = build_report(self.cfg, self.store)["accounts"]["cex"]["forward_metrics"]
        self.assertTrue(report["no_fills"])
        self.assertGreater(report["mark_age_s"], 89*86400)

    def test_maintenance_does_not_override_disabled_startup_download(self):
        self.cfg["bootstrap_on_start"] = False
        with patch("twobots.runtime.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep, \
                patch("twobots.runtime.maintain") as maintenance:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(maintenance_loop(self.cfg, self.store, Mock()))
        sleep.assert_awaited_once_with(3600)
        maintenance.assert_not_called()

    def test_nonfinite_and_inverted_risk_config_rejected(self):
        for changes in ({"fee_rate": float("nan")}, {"min_stop_fraction": .5, "max_stop_fraction": .1}):
            cfg = copy.deepcopy(self.cfg)
            cfg["cex"].update(changes)
            path = self.store.root / "invalid.yaml"
            path.write_text(yaml.safe_dump(cfg))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                load_config(path)

    def test_invalid_ohlcv_is_not_persisted(self):
        ts = int(time.time()//300)*300-600
        http = Mock()
        http.json.return_value = {"data": {"attributes": {"ohlcv_list": [
            [ts, 1, 2, .5, 1, -1], [ts, 1, float("nan"), .5, 1, 1],
            [ts+1, 1, 2, .5, 1, 1], [ts, 1, 2, .5, 1, 10]]}}}
        Fetcher(self.cfg, self.store, http).pool_history("solana", "P", refresh=True)
        self.assertEqual(len(self.store.candles("dex:solana", "P")), 1)

    def test_http_redirect_does_not_send_credentials_or_use_extra_budget(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-target")
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/source"
            with self.assertRaisesRegex(ValueError, "redirect"):
                HTTP(self.cfg, self.store).request(url, headers={"X-API-KEY": "fixture-only"})
            self.assertEqual(requests, ["/source"])
            self.assertEqual(self.store.rows("SELECT n FROM requests")[0]["n"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
