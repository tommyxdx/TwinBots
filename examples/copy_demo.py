"""Offline copy-trading walkthrough. Run: python -m examples.copy_demo

Ranks four synthetic wallets, follows the ones that survive, and mirrors a
scripted fill stream into the paper account. Quotes come from a constant-price
stub, so nothing here is a backtest or an expected return.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from twobots.config import load_config
from twobots.follow import ActivityFeed, CopyTrader
from twobots.report import export_report
from twobots.storage import Store
from twobots.wallet_scanner import WalletScanner
from twobots.wallets import BASE58, DAY

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DECIMALS = 10 ** 6
PRICE_USD = 0.001


def address(number):
    raw = number.to_bytes(32, "big")
    encoded, n = "", number
    while n:
        n, digit = divmod(n, 58)
        encoded = BASE58[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def ledger(number, style, now):
    """Four archetypes the ranking is supposed to separate."""
    txs, marks = [], []
    for i in range(30):
        ts = now - 85 * DAY + i * 2 * DAY
        pnl = {"steady": 400 if i % 10 < 3 else -100,
               "jackpot": 10000 if i == 0 else -10,
               "high_win_loser": 10 if i % 10 < 9 else -200,
               "bag_holder": 400}[style]
        token = address(100 + i % 10)
        txs.append({"id": f"{i}:b", "ts": ts, "token": token, "side": "buy",
                    "quantity": "1", "notional_usd": "500", "fee_usd": "0"})
        txs.append({"id": f"{i}:s", "ts": ts + 3600, "token": token, "side": "sell",
                    "quantity": "1", "notional_usd": str(500 + pnl), "fee_usd": "0"})
    if style == "bag_holder":
        # Realises every winner, never closes a loser. Looks excellent on realised PnL.
        for j in range(30):
            txs.append({"id": f"bag:{j}", "ts": now - 40 * DAY + j * 3600, "token": address(300 + j),
                        "side": "buy", "quantity": "1", "notional_usd": "500", "fee_usd": "0"})
            marks.append({"token": address(300 + j), "quantity": "1", "value_usd": "0", "asof": now - 60})
    return {"schema_version": 1, "currency": "USD", "network": "solana", "address": address(number),
            "source": "SYNTHETIC_TEST_ONLY:" + style, "asof": now, "history_start": now - 120 * DAY,
            "quality": {k: True for k in ("complete", "initial_inventory_empty", "all_protocols",
                                          "fees_included", "transfers_included",
                                          "failed_transactions_included")},
            "transactions": sorted(txs, key=lambda r: r["ts"]), "marks": marks}


class StubGateway:
    """Constant price. A real route would move against the size and the delay."""

    def quote(self, input_token, output_token, amount):
        rate = PRICE_USD ** -1 if input_token == USDC else PRICE_USD
        out = int(amount / DECIMALS * rate * DECIMALS)
        return {"input_token": input_token, "output_token": output_token, "in_amount": amount,
                "out_amount": out, "min_out": int(out * 0.99), "price_impact_fraction": 0.001,
                "asof": time.time(), "http_latency_s": 0.0}


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("Offline copy demo must never access a network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="copy_demo_output")
    args = parser.parse_args()
    now = time.time()
    root = Path(args.output).resolve()
    ledgers, activity = root / "ledgers", root / "activity"
    ledgers.mkdir(parents=True, exist_ok=True)
    activity.mkdir(parents=True, exist_ok=True)

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    cfg["data_dir"] = str(root)
    cfg["wallets"].update(ledger_dir=str(ledgers), addresses=[], discover_enabled=False,
                          max_wallets_per_run=50)
    cfg["follow"].update(enabled=True, source="local", activity_dir=str(activity))
    cfg["dex"].update(dropped_probability=0, revert_probability=0, latency_ms=[0, 0])

    wallets = {500: "steady", 501: "jackpot", 502: "high_win_loser", 503: "bag_holder"}
    for number, style in wallets.items():
        data = ledger(number, style, now)
        (ledgers / (data["address"] + ".json")).write_text(json.dumps(data), encoding="utf-8")

    store = Store(root)
    try:
        ranking = WalletScanner(cfg, store, NoNetwork(), NoNetwork()).run_once()
        bot = CopyTrader(cfg, store, StubGateway(), ActivityFeed(cfg, NoNetwork()))
        leaders = bot.leaders(time.time())
        for leader in leaders:
            (activity / (leader + ".json")).write_text(json.dumps(
                {"schema_version": 1, "network": "solana", "address": leader, "asof": time.time(),
                 "source": "SYNTHETIC_TEST_ONLY",
                 "fills": [{"id": "entry", "ts": time.time() - 3, "side": "buy",
                            "token": address(900), "notional_usd": "250"},
                           {"id": "too_late", "ts": time.time() - 3600, "side": "buy",
                            "token": address(901), "notional_usd": "250"}]}), encoding="utf-8")
        asyncio.run(bot.step())
        account = bot.ledger.state()
        print(json.dumps({
            "synthetic_only": True,
            "ranking": [{"style": r["source"].split(":")[-1], "status": r["status"],
                         "score": r["score"], "flags": r["flags"]} for r in ranking["ranking"]],
            "followed": leaders,
            "positions": {t: {"leader": p.get("leader"), "copy_lag_s": p.get("copy_lag_s"),
                              "cost_usd": p["cost"]} for t, p in account["positions"].items()},
            "stale_signal_ignored": address(901) not in account["positions"],
            "cash": account["cash"],
            "report": export_report(cfg, store),
        }, indent=2, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
