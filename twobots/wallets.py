"""Deterministic wallet accounting and ranking. No model, signing or trading.

The input is a normalized, fully paginated spot ledger, not a provider's PnL
summary. Completeness remains a claim by the data source, exposed in the report.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import math
import re
import time

DAY = 86400
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def valid_address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value):
        return False
    n = 0
    for ch in value:
        n = n * 58 + BASE58.index(ch)
    size = (n.bit_length() + 7) // 8 + len(value) - len(value.lstrip("1"))
    return size == 32


def decimal(value, name, minimum=Decimal(0)):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid {name}") from None
    # Bound adversarial exponents and float conversion used for display/scoring.
    if (not number.is_finite() or abs(number) > Decimal("1e40") or number < minimum
            or (number != 0 and abs(number) < Decimal("1e-30"))):
        raise ValueError(f"Invalid {name}")
    return number


def timestamp(value):
    n = float(decimal(value, "timestamp"))
    if n > 1e11:
        raise ValueError("Wallet timestamps must be Unix seconds")
    return n


def ratio(a, b):
    return float(a / b) if b else None


def analyze_ledger(data, address, network="solana", now=None, max_age_s=21600):
    now = time.time() if now is None else now
    if network != "solana" or not valid_address(address):
        raise ValueError("Wallet ranking currently requires a Solana public address")
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1
            or data.get("network") != network or data.get("address") != address or data.get("currency") != "USD"):
        raise ValueError("Wallet schema/network/address mismatch")
    end, start = timestamp(data["asof"]), timestamp(data["history_start"])
    if end > now + 60 or now - end > max_age_s or start > end:
        raise ValueError("Wallet history is stale or has invalid timestamps")
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Wallet data must identify its source")
    quality = data.get("quality", {})
    if not isinstance(quality, dict):
        raise ValueError("Wallet quality must be an object")
    required = ("complete", "initial_inventory_empty", "all_protocols", "fees_included",
                "transfers_included", "failed_transactions_included")
    missing = [name for name in required if quality.get(name) is not True]
    if missing:
        raise ValueError("Unverified ledger coverage: " + ", ".join(missing))
    if start > now - 90 * DAY:
        raise ValueError("Need at least 90 days of complete history and earlier cost basis")
    rows = data.get("transactions")
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ValueError("Expected at most 50000 fully paginated transactions")
    positions, sales, cycles, expenses, activity, seen = {}, [], [], [], [], set()
    previous = start
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each transaction must be an object")
        ident = row.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("Missing or duplicate transaction ID")
        seen.add(ident)
        ts = timestamp(row["ts"])
        if not previous <= ts <= min(end, now):
            raise ValueError("Ledger must be chronological, including same-time execution order")
        previous = ts
        side = row.get("side")
        fee = decimal(row.get("fee_usd"), "fee_usd")
        if side == "fee":
            expenses.append((ts, fee))
            continue
        # Transfers/airdrops are not zero-cost buys. Reconciliation is required
        # before a source can produce a complete trading ledger for this wallet.
        if side not in ("buy", "sell"):
            raise ValueError("Unsupported transfer/airdrop/activity; reconcile cost basis before ranking")
        token = row.get("token")
        if not valid_address(token):
            raise ValueError("Invalid token mint address")
        qty = decimal(row.get("quantity"), "quantity")
        amount = decimal(row.get("notional_usd"), "notional_usd")
        if qty <= 0 or (side == "buy" and amount <= 0):
            raise ValueError("Trade requires positive quantity and a known buy cost")
        activity.append((ts, token))
        if side == "buy":
            if token not in positions:
                positions[token] = {"quantity": Decimal(0), "cost": Decimal(0),
                                    "pnl": Decimal(0), "sold_cost": Decimal(0), "opened": ts}
            p = positions[token]
            p["quantity"] += qty
            p["cost"] += amount + fee
        else:
            p = positions.get(token)
            if p is None or qty > p["quantity"]:
                raise ValueError("Sell exceeds known inventory; missing purchases or transferred tokens")
            cost = p["cost"] * qty / p["quantity"]
            pnl = amount - fee - cost
            p["quantity"] -= qty
            p["cost"] -= cost
            p["pnl"] += pnl
            p["sold_cost"] += cost
            sales.append({"ts": ts, "token": token, "pnl": pnl, "cost": cost})
            if p["quantity"] == 0:
                cycles.append({"ts": ts, "opened": p["opened"], "token": token,
                               "pnl": p["pnl"], "cost": p["sold_cost"]})
                del positions[token]
    marks = data.get("marks")
    if not isinstance(marks, list):
        raise ValueError("Explicit current inventory marks required (empty list if flat)")
    mark_map = {}
    for mark in marks:
        if not isinstance(mark, dict):
            raise ValueError("Each mark must be an object")
        token = mark.get("token")
        if token in mark_map or token not in positions:
            raise ValueError("Duplicate or unmatched inventory mark")
        mark_ts = timestamp(mark["asof"])
        if not end - 3600 <= mark_ts <= min(end, now):
            raise ValueError("Inventory marks must be within one hour of ledger asof")
        if decimal(mark["quantity"], "mark quantity") != positions[token]["quantity"]:
            raise ValueError("Inventory mark quantity does not reconcile")
        mark_map[token] = decimal(mark["value_usd"], "value_usd")
    if set(mark_map) != set(positions):
        raise ValueError("Missing valuation for open inventory; do not omit losing tokens")
    open_cost = sum((p["cost"] for p in positions.values()), Decimal(0))
    open_pnl = sum((mark_map[t] - p["cost"] for t, p in positions.items()), Decimal(0))
    # Winning open positions cannot cancel the penalty for open losers.
    open_loss = sum((min(Decimal(0), mark_map[t] - p["cost"]) for t, p in positions.items()), Decimal(0))
    windows = {}
    for days in (7, 30, 90):
        cutoff = now - days * DAY
        realized = [s for s in sales if s["ts"] >= cutoff]
        completed = [c for c in cycles if c["opened"] >= cutoff]
        cost = sum((s["cost"] for s in realized), Decimal(0))
        standalone_fees = sum((f for ts, f in expenses if ts >= cutoff), Decimal(0))
        pnl = sum((s["pnl"] for s in realized), Decimal(0)) - standalone_fees
        wins = [c["pnl"] for c in completed if c["pnl"] > 0]
        losses = [-c["pnl"] for c in completed if c["pnl"] < 0]
        gross_win, gross_loss = sum(wins, Decimal(0)), sum(losses, Decimal(0))
        by_token = defaultdict(lambda: {"pnl": Decimal(0), "cost": Decimal(0)})
        by_week = defaultdict(Decimal)
        for s in realized:
            by_token[s["token"]]["pnl"] += s["pnl"]
            by_token[s["token"]]["cost"] += s["cost"]
            by_week[int(s["ts"] // (7 * DAY))] += s["pnl"]
        for ts, fee in expenses:
            if ts >= cutoff:
                by_week[int(ts // (7 * DAY))] -= fee
        best = max(by_token, key=lambda t: (by_token[t]["pnl"], t), default=None)
        best_data = by_token[best] if best is not None else {"pnl": Decimal(0), "cost": Decimal(0)}
        positive_tokens = sum((max(Decimal(0), r["pnl"]) for r in by_token.values()), Decimal(0))
        n = len(completed)
        mean_win = gross_win / len(wins) if wins else Decimal(0)
        mean_loss = gross_loss / len(losses) if losses else Decimal(0)
        active = [(ts, t) for ts, t in activity if ts >= cutoff]
        windows[str(days)] = {
            "realized_pnl_usd": float(pnl), "sold_cost_usd": float(cost), "cost_roi": ratio(pnl, cost),
            "trade_fills": len(active), "sell_fills": len(realized), "closed_cycles": n,
            "cross_window_cycles_excluded": sum(c["ts"] >= cutoff > c["opened"] for c in cycles),
            "wins": len(wins), "losses": len(losses), "breakeven": n - len(wins) - len(losses),
            "win_rate": len(wins) / n if n else None,
            "average_win_usd": float(mean_win), "average_loss_usd": float(mean_loss),
            "payoff_ratio": ratio(mean_win, mean_loss), "profit_factor": ratio(gross_win, gross_loss),
            "no_observed_losses": gross_loss == 0,
            "cycle_expectancy_usd": float((gross_win - gross_loss) / n) if n else None,
            "cycle_profit_balance": ratio(gross_win - gross_loss, gross_win + gross_loss) or 0.0,
            "closed_tokens": len({c["token"] for c in completed}),
            "active_days": len({int(ts // DAY) for ts, _ in active}),
            "active_weeks": len({int(ts // (7 * DAY)) for ts, _ in active}),
            "profitable_realization_weeks": sum(v > 0 for v in by_week.values()),
            "realization_weeks": len(by_week), "best_token": best,
            "best_token_profit_share": ratio(max(Decimal(0), best_data["pnl"]), positive_tokens),
            "without_best_token_pnl_usd": float(pnl - best_data["pnl"]),
            "without_best_token_roi": ratio(pnl - best_data["pnl"], cost - best_data["cost"]),
            "inventory_stressed_roi": ratio(pnl + open_loss, cost + open_cost),
            "standalone_fees_usd": float(standalone_fees),
        }
    return {"address": address, "network": network, "asof": end, "source": source,
            "coverage": quality, "accounting": "moving_weighted_average_after_fees",
            "open_cost_usd": float(open_cost), "unrealized_pnl_usd": float(open_pnl),
            "open_loss_usd": float(open_loss), "open_tokens": len(positions), "windows": windows}


def rank_wallets(analyses, min_cycles=10, min_tokens=3):
    results = []
    for item in analyses:
        result = dict(item)
        m = item["windows"]["90"]
        flags = []
        if m["closed_cycles"] < min_cycles or m["closed_tokens"] < min_tokens:
            flags.append("insufficient_independent_sample")
        if m["best_token_profit_share"] is not None and m["best_token_profit_share"] > .6:
            flags.append("profit_concentrated_in_one_token")
        if m["without_best_token_pnl_usd"] <= 0:
            flags.append("not_profitable_without_best_token")
        if m["realized_pnl_usd"] + item["open_loss_usd"] <= 0:
            flags.append("realized_profit_does_not_cover_open_losses")
        confidence = (m["closed_cycles"] / (m["closed_cycles"] + 20)
                      * min(1, m["closed_tokens"] / 8) ** .5
                      * min(1, m["active_weeks"] / 6) ** .5)
        transform = lambda value: math.tanh((value or 0) / .2)
        edge = (.45 * transform(m["cost_roi"]) + .25 * transform(m["without_best_token_roi"])
                + .20 * transform(m["inventory_stressed_roi"]) + .10 * m["cycle_profit_balance"])
        # A penalty can only reduce a positive score, never improve a loser.
        concentration = 1 - .7 * (m["best_token_profit_share"] or 0)
        score = 100 * confidence * (edge * concentration if edge > 0 else edge)
        eligible = "insufficient_independent_sample" not in flags and m["cost_roi"] is not None
        result.update(score=round(score, 6) if eligible else None, sample_weight=round(confidence, 6),
                      status="ranked" if eligible else "observation", flags=flags,
                      score_meaning="Unvalidated historical research priority, not profit probability")
        results.append(result)
    results.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), x["address"]))
    rank = 0
    for result in results:
        if result["score"] is not None:
            rank += 1
            result["rank"] = rank
        else:
            result["rank"] = None
    return results
