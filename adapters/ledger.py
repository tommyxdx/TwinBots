"""Build a complete normalized ledger for one wallet.

    python -m adapters.ledger --address <pubkey> --out wallet_ledgers

The ledger is written only if every transaction touching a research token could
be reconstructed. A wallet with transfers, airdrops or token-for-token swaps has
an unknowable cost basis, so the adapter reports why and writes nothing rather
than inventing a zero-cost entry that would flatter the wallet's record.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

from .helius import Helius
from .prices import SolPrice, quote_pricer
from .reconstruct import ledger_rows, token_decimals

DAY = 86400
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUPITER_QUOTE = "https://api.jup.ag/swap/v1/quote"
QUALITY = ("complete", "initial_inventory_empty", "all_protocols", "fees_included",
           "transfers_included", "failed_transactions_included")


def inventory(rows):
    """Replay the rows exactly as the bot will, so marks reconcile to the cent."""
    held = {}
    for row in rows:
        if row["side"] not in ("buy", "sell"):
            continue
        quantity = Decimal(row["quantity"])
        token = row["token"]
        held[token] = held.get(token, Decimal(0)) + (quantity if row["side"] == "buy" else -quantity)
    return {token: amount for token, amount in held.items() if amount != 0}


def jupiter_value_usd(mint, quantity, decimals, timeout=20):
    """Executable USD value of held inventory, or None when nothing will buy it.

    A routed quote is used rather than an oracle price because the ranking cares
    what the position could actually be exited for.
    """
    raw = int(quantity * (Decimal(10) ** decimals))
    if raw <= 0:
        return None
    query = urllib.parse.urlencode({"inputMint": mint, "outputMint": USDC, "amount": str(raw),
                                    "slippageBps": 100, "swapMode": "ExactIn"})
    request = urllib.request.Request(JUPITER_QUOTE + "?" + query,
                                     headers={"User-Agent": "TwinCryptoBots-Adapter/1.2.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(1024 * 1024))
    except Exception:
        return None
    if not payload.get("routePlan"):
        return None
    return Decimal(payload["outAmount"]) / (Decimal(10) ** 6)


def build(address, helius, sol_price, now=None, quote_marks=True, max_pages=400):
    now = time.time() if now is None else now
    entries = list(helius.transactions(address, max_pages=max_pages))
    if not entries:
        raise ValueError("No transactions found for this address")
    first = min(int(e["blockTime"]) for e in entries)
    if first > now - 90 * DAY:
        raise ValueError(f"Wallet history starts {int((now - first) / DAY)} days ago; "
                         "the ranking needs 90 days plus earlier cost basis")
    sol_price.load(first - 3600, now)
    rows, rejected = ledger_rows(entries, address, quote_pricer(sol_price))
    if rejected:
        reasons = {}
        for item in rejected:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        raise ValueError("Cost basis cannot be reconstructed: "
                         + ", ".join(f"{k}x{v}" for k, v in sorted(reasons.items()))
                         + f" (first at {min(i['ts'] for i in rejected)})")
    decimals = token_decimals(entries, address)
    marks, unpriced = [], []
    for token, quantity in sorted(inventory(rows).items()):
        if quantity < 0:
            raise ValueError(f"Negative inventory in {token}: history is incomplete")
        value = jupiter_value_usd(token, quantity, decimals.get(token, 0)) if quote_marks else None
        if value is None:
            # Retained at zero, never dropped: deleting a dead bag would erase the loss.
            unpriced.append(token)
            value = Decimal(0)
        marks.append({"token": token, "quantity": str(quantity), "value_usd": str(value),
                      "asof": int(now)})
    return {"schema_version": 1, "currency": "USD", "network": "solana", "address": address,
            "source": "helius-getTransactionsForAddress+binance-SOLUSDT-1m+jupiter-marks/1.2.0",
            "history_start": first - 1, "asof": int(now),
            "quality": {name: True for name in QUALITY},
            "transactions": rows, "marks": marks}, {"transactions": len(entries),
            "ledger_rows": len(rows), "open_tokens": len(marks),
            "unpriced_inventory_marked_zero": unpriced, "rpc_calls": helius.calls}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", required=True, help="Solana wallet public key")
    parser.add_argument("--out", default="wallet_ledgers", help="Directory for <address>.json")
    parser.add_argument("--cache", default="data/downloads", help="Kline archive cache")
    parser.add_argument("--max-pages", type=int, default=400)
    parser.add_argument("--no-quote-marks", action="store_true",
                        help="Skip Jupiter and mark all open inventory at zero")
    args = parser.parse_args(argv)
    try:
        data, report = build(args.address, Helius(), SolPrice(args.cache),
                             quote_marks=not args.no_quote_marks, max_pages=args.max_pages)
    except Exception as exc:
        print(json.dumps({"address": args.address, "written": False,
                          "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / (args.address + ".json")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"address": args.address, "written": str(path), **report},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
