"""Build a complete normalized ledger for one wallet.

    python -m adapters.ledger --address <pubkey> --out wallet_ledgers

Transfers, airdrops, token-for-token swaps and batch exits are all recorded in a
form the accounting can hold open without inventing a cost basis for them. What
remains unusable is a transaction whose cost has no defensible allocation across
several acquired tokens; the adapter reports that and writes nothing, rather than
guessing a split that would decide the wallet's rank.
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

from twobots.config import load_env
from twobots.wallets import new_position, remove

from .helius import PAGE, Helius, TooMuchHistory
from .prices import SolPrice, quote_pricer
from .reconstruct import ledger_rows, token_decimals

DAY = 86400
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUPITER_QUOTE = "https://api.jup.ag/swap/v1/quote"
QUALITY = ("complete", "initial_inventory_empty", "all_protocols", "fees_included",
           "transfers_included", "failed_transactions_included")


def inventory(rows):
    """Replay quantities with the bot's own position arithmetic.

    The proportional split between bought and transferred-in coins is not
    reproducible by a simpler running total, and a mark that misses it by a
    rounding step is rejected outright, so the same helpers are reused here.
    """
    positions = {}
    for row in rows:
        side = row["side"]
        if side == "fee":
            continue
        if side in ("buy", "transfer_in"):
            position = positions.setdefault(row["token"], new_position(row["ts"]))
            position["traded_qty" if side == "buy" else "external_qty"] += Decimal(row["quantity"])
        elif side in ("sell", "transfer_out"):
            remove(positions[row["token"]], Decimal(row["quantity"]), side)
        elif side == "swap":
            out_qty, in_qty = Decimal(row["quantity_out"]), Decimal(row["quantity_in"])
            traded, _, _ = remove(positions[row["token_out"]], out_qty, "Swap")
            share = traded / out_qty if out_qty > 0 else Decimal(0)
            target = positions.setdefault(row["token_in"], new_position(row["ts"]))
            target["traded_qty"] += in_qty * share
            target["external_qty"] += in_qty * (Decimal(1) - share)
        elif side == "batch_sell":
            for leg in row["legs"]:
                remove(positions[leg["token"]], Decimal(leg["quantity"]), "Batch leg")
    return {token: p["traded_qty"] + p["external_qty"] for token, p in positions.items()
            if p["traded_qty"] > 0 or p["external_qty"] > 0}


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


def reason_counts(rejected):
    counts = {}
    for item in rejected:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return dict(sorted(counts.items()))


def build(address, helius, sol_price, now=None, quote_marks=True, max_pages=400,
          min_history_days=30):
    """-> (ledger or None, report). None means the wallet cannot be ranked honestly.

    Being unusable is an ordinary outcome, not an error, so the report always
    says how many transactions were seen and exactly what blocked the rest.
    """
    now = time.time() if now is None else now
    # Two signature-only pages price the wallet before its full history is bought.
    # A market maker's ledger costs the entire page budget and is then thrown away
    # at the cap, so the recent rate is extrapolated over the window first.
    size = helius.history_size(address)
    if not size["complete"] and size["oldest"]:
        per_day = size["transactions"] * DAY / max(now - size["oldest"], 1)
        projected = per_day * min_history_days
        if projected > max_pages * PAGE:
            return None, {"address": address, "rpc_calls": helius.calls, "usable": False,
                          "blocked_by": "history_exceeds_page_budget",
                          "transactions_per_day": round(per_day), "projected": round(projected)}
    try:
        entries = list(helius.transactions(address, max_pages=max_pages))
    except TooMuchHistory as exc:
        return None, {"address": address, "rpc_calls": helius.calls, "usable": False,
                      "blocked_by": "history_exceeds_page_budget", "detail": str(exc)}
    report = {"address": address, "transactions": len(entries), "rpc_calls": helius.calls}
    if not entries:
        return None, {**report, "usable": False, "blocked_by": "no_transactions"}
    first = min(int(e["blockTime"]) for e in entries)
    report["history_days"] = int((now - first) / DAY)
    if first > now - min_history_days * DAY:
        return None, {**report, "usable": False,
                      "blocked_by": f"history_shorter_than_{min_history_days}_days"}
    sol_price.load(first - 3600, now)
    rows, rejected = ledger_rows(entries, address, quote_pricer(sol_price))
    by_side = reason_counts([{"reason": r["side"]} for r in rows])
    report.update(usable_rows=len(rows), rows_by_side=by_side, unreconstructable=len(rejected),
                  reasons=reason_counts(rejected))
    if rejected:
        return None, {**report, "usable": False, "blocked_by": "unreconstructable_cost_basis",
                      "first_rejection_ts": min(i["ts"] for i in rejected)}
    decimals = token_decimals(entries, address)
    marks, unpriced = [], []
    for token, quantity in sorted(inventory(rows).items()):
        if quantity < 0:
            return None, {**report, "usable": False, "blocked_by": "negative_inventory",
                          "token": token}
        value = jupiter_value_usd(token, quantity, decimals.get(token, 0)) if quote_marks else None
        if value is None:
            # Retained at zero, never dropped: deleting a dead bag would erase the loss.
            unpriced.append(token)
            value = Decimal(0)
        marks.append({"token": token, "quantity": str(quantity), "value_usd": str(value),
                      "asof": int(now)})
    ledger = {"schema_version": 1, "currency": "USD", "network": "solana", "address": address,
              "source": "helius-getTransactionsForAddress+binance-SOLUSDT-1m+jupiter-marks/1.2.0",
              "history_start": first - 1, "asof": int(now),
              "quality": {name: True for name in QUALITY},
              "transactions": rows, "marks": marks}
    return ledger, {**report, "usable": True, "open_tokens": len(marks),
                    "unpriced_inventory_marked_zero": unpriced}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", action="append", required=True,
                        help="Solana wallet public key; repeat to survey several")
    parser.add_argument("--out", default="wallet_ledgers", help="Directory for <address>.json")
    parser.add_argument("--cache", default="data/downloads", help="Kline archive cache")
    parser.add_argument("--max-pages", type=int, default=400)
    parser.add_argument("--min-history-days", type=int, default=30,
                        help="Match wallets.min_history_days in config.yaml")
    parser.add_argument("--no-quote-marks", action="store_true",
                        help="Skip Jupiter and mark all open inventory at zero")
    parser.add_argument("--diagnose", action="store_true",
                        help="Report why each wallet is or is not usable; write nothing")
    parser.add_argument("--env", default=".env", help="File to read HELIUS_API_KEY from")
    args = parser.parse_args(argv)
    load_env(Path(args.env))
    helius, sol_price = Helius(), SolPrice(args.cache)
    out = Path(args.out)
    reports, written = [], 0
    for address in args.address:
        try:
            ledger, report = build(address, helius, sol_price,
                                   quote_marks=not args.no_quote_marks and not args.diagnose,
                                   max_pages=args.max_pages,
                                   min_history_days=args.min_history_days)
        except Exception as exc:
            reports.append({"address": address, "usable": False, "error": str(exc)})
            continue
        if ledger is not None and not args.diagnose:
            out.mkdir(parents=True, exist_ok=True)
            path = out / (address + ".json")
            path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
            report["written"] = str(path)
            written += 1
        reports.append(report)
    usable = sum(1 for r in reports if r.get("usable"))
    summary = {"checked": len(reports), "usable": usable, "written": written,
               "rejection_rate": round(1 - usable / len(reports), 3) if reports else None,
               "rpc_calls": helius.calls, "wallets": reports}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if usable else 1


if __name__ == "__main__":
    sys.exit(main())
