"""Offline synthetic scenarios. Run: python -m examples.wallet_demo"""
import argparse
import json
from pathlib import Path
import time

from twobots.config import load_config
from twobots.report import export_report
from twobots.storage import Store
from twobots.wallet_scanner import WalletScanner
from twobots.wallets import BASE58, DAY


def address(number):
    raw = number.to_bytes(32, "big")
    encoded = ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def ledger(number=500, style="low_win", scale=1, now=None):
    now = time.time() if now is None else now
    txs = []
    for i in range(30):
        ts = now - 85 * DAY + i * 2 * DAY
        pnl = 400 if i % 10 < 3 else -100
        if style == "jackpot":
            pnl = 10000 if i == 0 else -10
        elif style == "high_win_loss":
            pnl = 10 if i % 10 < 9 else -200
        cost = 500 * scale
        for side, amount, offset in (("buy", cost, 0), ("sell", cost + pnl * scale, 3600)):
            txs.append({"id": f"{i}:{side}", "ts": ts + offset, "token": address(i % 10 + 100),
                        "side": side, "quantity": "1", "notional_usd": str(amount), "fee_usd": "0"})
    return {"schema_version": 1, "currency": "USD", "network": "solana", "address": address(number),
            "source": "SYNTHETIC_TEST_ONLY:" + style, "asof": now, "history_start": now - 100 * DAY,
            "quality": {k: True for k in ("complete", "initial_inventory_empty", "all_protocols", "fees_included",
                                         "transfers_included", "failed_transactions_included")},
            "transactions": txs, "marks": []}


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("Offline wallet demo must never access a network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="wallet_demo_output")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    folder = root / "ledgers"
    folder.mkdir(parents=True, exist_ok=True)
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    cfg["data_dir"] = str(root)
    cfg["wallets"].update(ledger_dir=str(folder), addresses=[], discover_enabled=False, max_wallets_per_run=50)
    for number, style, scale in ((500, "low_win", 1), (501, "low_win", 100),
                                 (502, "jackpot", 1), (503, "high_win_loss", 1)):
        data = ledger(number, style, scale)
        (folder / (data["address"] + ".json")).write_bytes(json.dumps(data).encode())
    store = Store(root)
    try:
        result = WalletScanner(cfg, store, NoNetwork(), NoNetwork()).run_once()
        print(json.dumps({"synthetic_only": True, "report": export_report(cfg, store),
                          "wallets": [{"source": r["source"], "score": r["score"], "metrics": r["windows"]["90"]}
                                      for r in result["ranking"]]}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
