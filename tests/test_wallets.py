"""Offline financial-accounting counterexamples, never trading performance evidence."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from examples.wallet_demo import address, ledger, NoNetwork
from twobots.cli import services
from twobots.config import load_config
from twobots.report import build_report, export_report
from twobots.runtime import maintain
from twobots.storage import Store
from twobots.wallet_scanner import WalletScanner
from twobots.wallets import analyze_ledger, rank_wallets, valid_address, DAY

ROOT = Path(__file__).resolve().parents[1]
NOW = 1900000000


def analyze(data):
    return analyze_ledger(data, data["address"], now=NOW)


class WalletAccounting(unittest.TestCase):
    def test_solana_address_decodes_to_32_bytes(self):
        self.assertTrue(valid_address(address(500)))
        for value in ("0" * 44, "1" * 44, "../../secret", None, "hello"):
            self.assertFalse(valid_address(value))

    def test_low_win_profitable_beats_high_win_loser(self):
        good = analyze(ledger(now=NOW))
        bad = analyze(ledger(501, "high_win_loss", now=NOW))
        m = good["windows"]["90"]
        self.assertEqual(m["realized_pnl_usd"], 1500)
        self.assertEqual(m["win_rate"], .3)
        self.assertAlmostEqual(m["profit_factor"], 12 / 7)
        ranked = rank_wallets([bad, good])
        self.assertEqual(ranked[0]["address"], good["address"])
        # A wallet that is net down is refused a rank outright, not merely scored low.
        self.assertIsNone(ranked[1]["score"])
        self.assertEqual(ranked[1]["status"], "observation")

    def test_capital_scale_does_not_improve_score(self):
        small = analyze(ledger(now=NOW))
        large = analyze(ledger(501, scale=100, now=NOW))
        ranks = rank_wallets([small, large])
        self.assertEqual(ranks[0]["score"], ranks[1]["score"])
        self.assertEqual(large["windows"]["90"]["realized_pnl_usd"], 100 * small["windows"]["90"]["realized_pnl_usd"])

    def test_one_token_jackpot_does_not_outrank_distributed_edge(self):
        good = analyze(ledger(now=NOW))
        lucky = analyze(ledger(501, "jackpot", now=NOW))
        self.assertGreater(lucky["windows"]["90"]["realized_pnl_usd"], good["windows"]["90"]["realized_pnl_usd"])
        ranked = rank_wallets([lucky, good])
        self.assertEqual(ranked[0]["address"], good["address"])
        self.assertIn("not_profitable_without_best_token", ranked[1]["flags"])

    def test_wac_allocates_buy_and_sell_fees_without_future_buy_rewrite(self):
        data = ledger(now=NOW)
        base = data["transactions"][0]
        data["transactions"] = [dict(base, id="1", quantity="10", notional_usd="100", fee_usd="2"),
                                dict(base, id="2", ts=NOW-1000, side="sell", quantity="4", notional_usd="60", fee_usd="1"),
                                dict(base, id="3", ts=NOW-500, quantity="4", notional_usd="80", fee_usd="0")]
        data["marks"] = [{"token": base["token"], "asof": NOW, "quantity": "10", "value_usd": "141.2"}]
        report = analyze(data)
        self.assertAlmostEqual(report["windows"]["90"]["realized_pnl_usd"], 18.2)
        self.assertAlmostEqual(report["open_cost_usd"], 141.2)
        self.assertEqual(report["windows"]["90"]["closed_cycles"], 0)

    def test_partial_sell_splitting_does_not_inflate_wins_or_score(self):
        data = ledger(now=NOW)
        original = analyze(data)
        txs = []
        for row in data["transactions"]:
            if row["side"] == "buy":
                txs.append(row)
            else:
                for i in range(10):
                    txs.append(dict(row, id=row["id"] + f":{i}", quantity="0.1",
                                    notional_usd=str(float(row["notional_usd"]) / 10)))
        data["transactions"] = txs
        split = analyze(data)
        self.assertEqual(split["windows"]["90"]["closed_cycles"], 30)
        self.assertEqual(split["windows"]["90"]["sell_fills"], 300)
        self.assertEqual(rank_wallets([original])[0]["score"], rank_wallets([split])[0]["score"])

    def test_open_losers_reduce_score_and_open_winners_cannot_cancel(self):
        data = ledger(now=NOW)
        before = rank_wallets([analyze(data)])[0]["score"]
        for i, value, cost in ((800, "0", "1000"), (801, "10000", "3000")):
            data["transactions"].append({"id": str(i), "ts": NOW-100, "side": "buy", "token": address(i),
                                         "quantity": "1", "notional_usd": cost, "fee_usd": "0"})
            data["marks"].append({"token": address(i), "quantity": "1", "value_usd": value, "asof": NOW})
        result = rank_wallets([analyze(data)])[0]
        # Realised 1500 still covers the 1000 open loss, so this stays ranked but scores lower.
        self.assertEqual(result["open_loss_usd"], -1000)
        self.assertGreater(result["unrealized_pnl_usd"], 0)
        self.assertLess(result["score"], before)
        self.assertNotIn("realized_profit_does_not_cover_open_losses", result["flags"])

    def test_open_losses_exceeding_realized_profit_block_the_ranking(self):
        """Selling winners while holding losers must not out-rank an honest wallet."""
        data = ledger(now=NOW)
        data["transactions"].append({"id": "bag", "ts": NOW-100, "side": "buy", "token": address(800),
                                     "quantity": "1", "notional_usd": "3000", "fee_usd": "0"})
        data["marks"].append({"token": address(800), "quantity": "1", "value_usd": "0", "asof": NOW})
        result = rank_wallets([analyze(data)])[0]
        self.assertEqual(result["windows"]["90"]["realized_pnl_usd"] + result["open_loss_usd"], -1500)
        self.assertIn("realized_profit_does_not_cover_open_losses", result["flags"])
        self.assertIsNone(result["score"])
        self.assertIsNone(result["rank"])

    def test_failed_transaction_fees_reduce_realized_profit(self):
        data = ledger(now=NOW)
        data["transactions"].append({"id": "failed", "ts": NOW-1, "side": "fee", "fee_usd": "100"})
        self.assertEqual(analyze(data)["windows"]["90"]["realized_pnl_usd"], 1400)

    def test_cross_window_cycle_uses_old_cost_but_not_partial_window_win_rate(self):
        data = ledger(now=NOW)
        buy, sell = data["transactions"][:2]
        data["transactions"] = [dict(buy, ts=NOW-95*DAY), dict(sell, ts=NOW-1)]
        result = analyze(data)["windows"]["90"]
        self.assertEqual(result["realized_pnl_usd"], 400)
        self.assertEqual(result["closed_cycles"], 0)
        self.assertEqual(result["cross_window_cycles_excluded"], 1)
        self.assertIsNone(result["win_rate"])

    def test_unknown_or_false_coverage_never_ranks(self):
        for field in ledger(now=NOW)["quality"]:
            for value in (False, "true", None):
                with self.subTest(field=field, value=value):
                    data = ledger(now=NOW)
                    data["quality"][field] = value
                    with self.assertRaises(ValueError):
                        analyze(data)

    def test_unknown_side_and_unbacked_sale_are_rejected(self):
        for side in ("airdrop", "sell"):
            data = ledger(now=NOW)
            data["transactions"][0]["side"] = side
            with self.assertRaises(ValueError):
                analyze(data)

    def test_transferred_in_inventory_is_not_trading_profit(self):
        """An airdrop sold for 5000 must not touch cost_roi, cycles or win rate."""
        clean = analyze(ledger(now=NOW))
        data = ledger(now=NOW)
        token = address(900)
        data["transactions"] += [
            {"id": "drop", "ts": NOW-200, "side": "transfer_in", "token": token,
             "quantity": "1", "fee_usd": "0"},
            {"id": "dump", "ts": NOW-100, "side": "sell", "token": token,
             "quantity": "1", "notional_usd": "5000", "fee_usd": "0"}]
        result = analyze(data)
        m, before = result["windows"]["90"], clean["windows"]["90"]
        self.assertEqual(m["external_origin_pnl_usd"], 5000)
        self.assertEqual(m["realized_pnl_usd"], before["realized_pnl_usd"])
        self.assertEqual(m["cost_roi"], before["cost_roi"])
        self.assertEqual(m["closed_cycles"], before["closed_cycles"])
        self.assertEqual(m["win_rate"], before["win_rate"])

    def test_token_swap_carries_basis_and_keeps_total_profit_exact(self):
        """Neither leg supplies a USD price, so nothing is realised at the swap.

        Buying A for 500, swapping all of it into B, then selling B for 900 must
        report the same 400 as buying A and selling it for 900 directly.
        """
        direct = ledger(now=NOW)
        a, b = address(900), address(901)
        direct["transactions"] += [
            {"id": "buy", "ts": NOW-5000, "side": "buy", "token": a,
             "quantity": "10", "notional_usd": "500", "fee_usd": "0"},
            {"id": "out", "ts": NOW-100, "side": "sell", "token": a,
             "quantity": "10", "notional_usd": "900", "fee_usd": "0"}]
        swapped = ledger(now=NOW)
        swapped["transactions"] += [
            {"id": "buy", "ts": NOW-5000, "side": "buy", "token": a,
             "quantity": "10", "notional_usd": "500", "fee_usd": "0"},
            {"id": "swap", "ts": NOW-3000, "side": "swap", "token_out": a, "quantity_out": "10",
             "token_in": b, "quantity_in": "4", "fee_usd": "0"},
            {"id": "out", "ts": NOW-100, "side": "sell", "token": b,
             "quantity": "4", "notional_usd": "900", "fee_usd": "0"}]
        one, two = analyze(direct)["windows"]["90"], analyze(swapped)["windows"]["90"]
        self.assertEqual(one["realized_pnl_usd"], two["realized_pnl_usd"])
        self.assertEqual(one["sold_cost_usd"], two["sold_cost_usd"])
        self.assertEqual(one["closed_cycles"], two["closed_cycles"])
        self.assertEqual(analyze(swapped)["censored_cost_fraction"], 0)

    def test_transfer_out_censors_the_cycle_instead_of_scoring_it(self):
        data = ledger(now=NOW)
        token = address(902)
        data["transactions"] += [
            {"id": "buy", "ts": NOW-5000, "side": "buy", "token": token,
             "quantity": "10", "notional_usd": "500", "fee_usd": "0"},
            {"id": "move", "ts": NOW-100, "side": "transfer_out", "token": token,
             "quantity": "10", "fee_usd": "0"}]
        base, result = analyze(ledger(now=NOW)), analyze(data)
        self.assertEqual(result["censored_cost_usd"], 500)
        self.assertGreater(result["censored_cost_fraction"], 0)
        # The outcome is unobservable, so it is neither a win nor a loss.
        self.assertEqual(result["windows"]["90"]["closed_cycles"],
                         base["windows"]["90"]["closed_cycles"])
        self.assertEqual(result["windows"]["90"]["realized_pnl_usd"],
                         base["windows"]["90"]["realized_pnl_usd"])
        self.assertEqual(result["open_tokens"], 0)

    def test_heavily_censored_record_cannot_rank(self):
        data = ledger(now=NOW)
        data["transactions"] += [
            {"id": f"b{i}", "ts": NOW-5000+i, "side": "buy", "token": address(910 + i),
             "quantity": "10", "notional_usd": "2000", "fee_usd": "0"} for i in range(12)]
        data["transactions"] += [
            {"id": f"m{i}", "ts": NOW-1000+i, "side": "transfer_out", "token": address(910 + i),
             "quantity": "10", "fee_usd": "0"} for i in range(12)]
        result = rank_wallets([analyze(data)])[0]
        self.assertGreater(result["censored_cost_fraction"], .25)
        self.assertIn("record_materially_censored", result["flags"])
        self.assertIsNone(result["score"])

    def test_batch_sell_splits_proceeds_by_cost_and_is_flagged(self):
        data = ledger(now=NOW)
        a, b = address(903), address(904)
        data["transactions"] += [
            {"id": "ba", "ts": NOW-5000, "side": "buy", "token": a,
             "quantity": "10", "notional_usd": "300", "fee_usd": "0"},
            {"id": "bb", "ts": NOW-4900, "side": "buy", "token": b,
             "quantity": "10", "notional_usd": "100", "fee_usd": "0"},
            {"id": "batch", "ts": NOW-100, "side": "batch_sell", "notional_usd": "800",
             "fee_usd": "0", "legs": [{"token": a, "quantity": "10"},
                                      {"token": b, "quantity": "10"}]}]
        result = rank_wallets([analyze(data)])[0]
        m = result["windows"]["90"]
        self.assertEqual(result["estimated_allocation_rows"], 1)
        self.assertIn("allocation_estimated", result["flags"])
        # 800 split 3:1 by basis gives both legs the same 100% return.
        self.assertAlmostEqual(m["realized_pnl_usd"], 1500 + 400)
        self.assertEqual(result["open_tokens"], 0)

    def test_history_floor_is_thirty_days_and_short_samples_are_downweighted(self):
        data = ledger(now=NOW)
        data["history_start"] = NOW - 40 * DAY
        data["transactions"] = [t for t in data["transactions"] if t["ts"] >= NOW - 35 * DAY]
        result = analyze(data)
        self.assertLess(result["windows"]["90"]["active_weeks"], 6)
        with self.assertRaises(ValueError):
            analyze_ledger(data, data["address"], now=NOW, min_history_days=90)

    def test_selling_all_but_a_crumb_closes_the_cycle(self):
        """Measured on real wallets: requiring exactly zero hid ten closed
        positions behind four, because traders leave dust and a proportional
        split of mixed-origin inventory leaves a rounding residue."""
        data = ledger(now=NOW)
        token = address(950)
        data["transactions"] += [
            {"id": "b", "ts": NOW-5000, "side": "buy", "token": token,
             "quantity": "1000", "notional_usd": "500", "fee_usd": "0"},
            {"id": "s", "ts": NOW-4000, "side": "sell", "token": token,
             "quantity": "999.9", "notional_usd": "900", "fee_usd": "0"}]
        data["marks"].append({"token": token, "quantity": "0.1", "value_usd": "0",
                              "asof": NOW})
        base = analyze(ledger(now=NOW))["windows"]["90"]
        m = analyze(data)["windows"]["90"]
        self.assertEqual(m["closed_cycles"], base["closed_cycles"] + 1)
        self.assertEqual(m["wins"], base["wins"] + 1)

    def test_selling_the_crumb_later_is_not_a_second_cycle(self):
        """A round trip needs a purchase; the leftover is not a new one."""
        data = ledger(now=NOW)
        token = address(951)
        data["transactions"] += [
            {"id": "b", "ts": NOW-5000, "side": "buy", "token": token,
             "quantity": "1000", "notional_usd": "500", "fee_usd": "0"},
            {"id": "s1", "ts": NOW-4000, "side": "sell", "token": token,
             "quantity": "999.9", "notional_usd": "900", "fee_usd": "0"},
            {"id": "s2", "ts": NOW-3000, "side": "sell", "token": token,
             "quantity": "0.1", "notional_usd": "0.01", "fee_usd": "0"}]
        base = analyze(ledger(now=NOW))["windows"]["90"]
        m = analyze(data)["windows"]["90"]
        self.assertEqual(m["closed_cycles"], base["closed_cycles"] + 1)
        self.assertEqual(m["losses"], base["losses"], "no micro-loss from dust")

    def test_a_genuinely_partial_exit_still_does_not_close(self):
        data = ledger(now=NOW)
        token = address(952)
        data["transactions"] += [
            {"id": "b", "ts": NOW-5000, "side": "buy", "token": token,
             "quantity": "1000", "notional_usd": "500", "fee_usd": "0"},
            {"id": "s", "ts": NOW-4000, "side": "sell", "token": token,
             "quantity": "500", "notional_usd": "450", "fee_usd": "0"}]
        data["marks"].append({"token": token, "quantity": "500", "value_usd": "400",
                              "asof": NOW})
        base = analyze(ledger(now=NOW))["windows"]["90"]
        self.assertEqual(analyze(data)["windows"]["90"]["closed_cycles"],
                         base["closed_cycles"], "half sold is not a round trip")

    def test_a_wallet_whose_activity_is_mostly_invisible_is_flagged(self):
        """A perp, loan or LP position never moves a token balance here, so its
        economics are simply absent. Measured on real wallets, a third to two
        thirds of their transactions classify as nothing at all."""
        data = ledger(now=NOW)
        visible = len(data["transactions"])
        data["classified_transactions"] = visible
        data["unclassified_transactions"] = visible * 3
        result = rank_wallets([analyze(data)])[0]
        self.assertGreater(result["windows"]["90"]["realized_pnl_usd"], 0)
        self.assertAlmostEqual(result["unclassified_fraction"], 0.75)
        self.assertIn("activity_partly_invisible", result["flags"])
        # Visible and profitable, so it still ranks; the copy trader refuses any
        # flag by default, which is where the caution belongs.
        self.assertIsNotNone(result["score"])

    def test_a_wallet_with_nothing_hidden_carries_no_such_flag(self):
        data = ledger(now=NOW)
        data["classified_transactions"] = len(data["transactions"])
        data["unclassified_transactions"] = 0
        result = rank_wallets([analyze(data)])[0]
        self.assertEqual(result["unclassified_fraction"], 0)
        self.assertNotIn("activity_partly_invisible", result["flags"])

    def test_invalid_identity_duplicate_and_nonfinite_amount_rejected(self):
        original = ledger(now=NOW)
        modifications = [lambda d: d.update(network="ethereum"), lambda d: d.update(asof=NOW+100),
                         lambda d: d.update(asof=NOW-30000), lambda d: d.update(history_start=NOW-10*DAY),
                         lambda d: d["transactions"][1].update(id=d["transactions"][0]["id"]),
                         lambda d: d["transactions"][1].update(notional_usd="NaN"),
                         lambda d: d["transactions"][1].update(fee_usd=True),
                         lambda d: d["transactions"][1].update(ts=NOW+1)]
        for mutate in modifications:
            data = deepcopy(original)
            mutate(data)
            with self.assertRaises(ValueError):
                analyze(data)

    def test_missing_or_inconsistent_inventory_mark_rejected(self):
        data = ledger(now=NOW)
        data["transactions"] = data["transactions"][:1]
        with self.assertRaises(ValueError):
            analyze(data)
        data["marks"] = [{"token": data["transactions"][0]["token"], "quantity": "2", "asof": NOW, "value_usd": "500"}]
        with self.assertRaises(ValueError):
            analyze(data)

    def test_currency_and_future_execution_are_not_silently_accepted(self):
        data = ledger(now=NOW)
        data["currency"] = "EUR"
        with self.assertRaises(ValueError):
            analyze(data)
        data = ledger(now=NOW)
        data["asof"] = NOW + 30
        data["transactions"][-1]["ts"] = NOW + 10
        with self.assertRaises(ValueError):
            analyze(data)

    def test_small_sample_stays_observation_even_with_extreme_profit(self):
        data = ledger(style="jackpot", now=NOW)
        data["transactions"] = data["transactions"][:2]
        result = rank_wallets([analyze(data)])[0]
        self.assertIsNone(result["rank"])
        self.assertIsNone(result["score"])

    def test_no_losses_produces_json_safe_metrics_without_infinite_confidence(self):
        data = ledger(now=NOW)
        for row in data["transactions"]:
            if row["side"] == "sell":
                row["notional_usd"] = "600"
        result = rank_wallets([analyze(data)])[0]
        self.assertIsNone(result["windows"]["90"]["profit_factor"])
        self.assertLess(result["sample_weight"], 1)
        json.dumps(result, allow_nan=False)


class WalletWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(ROOT / "config.example.yaml")
        self.cfg["data_dir"] = self.tmp.name
        self.cfg["wallets"].update(ledger_dir=str(Path(self.tmp.name) / "ledgers"),
                                   discover_enabled=False, addresses=[address(500)])
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, data):
        folder = Path(self.cfg["wallets"]["ledger_dir"])
        folder.mkdir(exist_ok=True)
        (folder / (data["address"] + ".json")).write_bytes(json.dumps(data).encode())

    def test_offline_scan_and_report_need_no_network_or_bootstrap(self):
        self.put(ledger(now=NOW))
        args = argparse.Namespace(command="scan", once=True)
        with patch("time.time", return_value=NOW), patch("twobots.cli.maintain") as maintenance, patch("twobots.cli.output"):
            asyncio.run(services(args, self.cfg, self.store, NoNetwork(), NoNetwork()))
            maintenance.assert_not_called()
            report = build_report(self.cfg, self.store)
            self.assertEqual(report["wallet_scanner"]["ranking"][0]["rank"], 1)
            self.assertFalse(report["wallet_scanner"]["stale"])
            self.assertIn("完整平仓", Path(export_report(self.cfg, self.store)).read_text(encoding="utf-8"))

    def test_http_adapter_cache_and_refresh_budget(self):
        self.cfg["wallets"].update(source="adapter", url_template="https://adapter.example/{network}/{address}")
        http = Mock()
        http.request.return_value = json.dumps(ledger(now=NOW)).encode()
        scanner = WalletScanner(self.cfg, self.store, http, NoNetwork())
        with patch("time.time", return_value=NOW):
            scanner.run_once()
            scanner.run_once()
        self.assertEqual(http.request.call_count, 1)

    def test_stale_cached_result_never_remains_ranked(self):
        self.cfg["wallets"].update(source="adapter", url_template="https://adapter.example/{address}", refresh_s=50000)
        http = Mock()
        http.request.return_value = json.dumps(ledger(now=NOW)).encode()
        scanner = WalletScanner(self.cfg, self.store, http, NoNetwork())
        with patch("time.time", return_value=NOW):
            scanner.run_once()
        with patch("time.time", return_value=NOW+22000):
            result = scanner.run_once()
        self.assertFalse(result["ranking"])
        self.assertEqual(len(result["unavailable"]), 1)
        self.assertEqual(http.request.call_count, 1)

    def test_invalid_local_refresh_removes_previous_good_result(self):
        data = ledger(now=NOW)
        self.put(data)
        scanner = WalletScanner(self.cfg, self.store, NoNetwork(), NoNetwork())
        with patch("time.time", return_value=NOW):
            self.assertTrue(scanner.run_once()["ranking"])
            data["quality"]["complete"] = False
            self.put(data)
            self.assertFalse(scanner.run_once()["ranking"])

    def test_wallet_refresh_rotates_and_is_bounded(self):
        self.cfg["wallets"].update(source="adapter", url_template="https://adapter.example/{address}",
                                   addresses=[address(500), address(501)], max_wallets_per_run=1)
        http = Mock()
        http.request.side_effect = [json.dumps(ledger(now=NOW)).encode(), json.dumps(ledger(501, now=NOW+1)).encode()]
        scanner = WalletScanner(self.cfg, self.store, http, NoNetwork())
        with patch("time.time", return_value=NOW):
            self.assertEqual(len(scanner.run_once()["ranking"]), 1)
        with patch("time.time", return_value=NOW+1):
            self.assertEqual(len(scanner.run_once()["ranking"]), 2)
        self.assertEqual(http.request.call_count, 2)

    def test_discovery_samples_large_sellers_and_caches_them(self):
        """Buyers may never close; only a seller shows behaviour the ranking can score.

        Sampling signers indiscriminately on new pools returned addresses that
        buy thousands of times and forward everything out, which no amount of
        reconstruction can turn into a record.
        """
        self.cfg["wallets"].update(discover_enabled=True, addresses=[], discovery_max_pools=1,
                                   discovery_addresses_per_pool=3, discovery_min_trade_usd=100,
                                   discovery_sells_only=True)
        http = Mock()
        http.json.return_value = {"data": [
            {"attributes": {"kind": "sell", "volume_in_usd": "900", "tx_from_address": address(501)}},
            {"attributes": {"kind": "sell", "volume_in_usd": "5000", "tx_from_address": address(500)}},
            {"attributes": {"kind": "sell", "volume_in_usd": "3", "tx_from_address": address(502)}},
            {"attributes": {"kind": "buy", "volume_in_usd": "9000", "tx_from_address": address(503)}},
            {"attributes": {"kind": "sell", "volume_in_usd": "400", "tx_from_address": "bad"}},
        ]}
        scanner = WalletScanner(self.cfg, self.store, http, NoNetwork())
        self.assertEqual(scanner.traders(address(100)), [address(500), address(501)],
                         "largest seller first, dust and buyers excluded")
        http.json.reset_mock()
        http.json.side_effect = [
            {"data": [{"attributes": {"address": address(100)}}]},
            {"data": [
                {"attributes": {"kind": "sell", "volume_in_usd": "5000", "tx_from_address": address(500)}},
                {"attributes": {"kind": "buy", "volume_in_usd": "9000", "tx_from_address": address(503)}},
            ]},
        ]
        with patch("time.time", return_value=NOW):
            result = scanner.run_once()
            scanner.run_once()
        self.assertEqual(http.json.call_count, 2, "the second pass reuses the cached round")
        self.assertEqual(result["candidate_count"], 1)
        self.assertFalse(result["ranking"])

    def test_wallet_maintenance_skips_token_models_and_history(self):
        fetcher = Mock()
        # Patched at the source: maintain imports these lazily so the science
        # stack stays out of a wallet-only process.
        with patch("twobots.models.train_cex") as cex, patch("twobots.models.train_scanner") as tokens:
            maintain(self.cfg, self.store, fetcher, True)
            cex.assert_called_once()
            tokens.assert_not_called()
            fetcher.follow_cohort.assert_not_called()

    def test_wallet_mode_rejects_dex_copy_execution_configuration(self):
        raw = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        raw = raw.replace("dex:\n  enabled: false", "dex:\n  enabled: true")
        path = Path(self.tmp.name) / "bad.yaml"
        path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "copy trading"):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
