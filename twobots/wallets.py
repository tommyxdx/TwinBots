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
DUST = Decimal("1e-24")
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TRADE_SIDES = ("buy", "sell", "fee", "transfer_in", "transfer_out", "swap", "batch_sell")


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


def new_position(ts):
    """Inventory split by origin. Externally received coins carry no cost basis."""
    return {"traded_qty": Decimal(0), "traded_cost": Decimal(0), "external_qty": Decimal(0),
            "peak_qty": Decimal(0), "pnl": Decimal(0), "sold_cost": Decimal(0),
            "funded": False, "opened": ts}


def exhausted(position, dust_fraction):
    """Has the traded side of this position effectively been exited?

    A trader who sells 99.98% and leaves a crumb has closed that position, and so
    has one whose exit leaves a rounding residue from splitting a mixed-origin
    holding. Requiring exactly zero counted neither: measured on real wallets it
    hid ten closed positions behind four, which is the difference between a
    wallet that plainly trades and one refused for an insufficient sample.
    """
    if position["traded_qty"] <= 0:
        return True
    return (position["peak_qty"] > 0
            and position["traded_qty"] <= position["peak_qty"] * dust_fraction)


def remove(position, quantity, what):
    """Take `quantity` out pro rata across both origins. -> (traded, cost, external).

    SPL tokens are fungible, so there is no fact about which coins moved; splitting
    by held quantity is the only neutral choice. A full exit is handled exactly so
    that closing a position still lands on zero rather than a rounding residue.
    """
    total = position["traded_qty"] + position["external_qty"]
    if quantity - total > DUST:
        raise ValueError(f"{what} exceeds known inventory; missing purchases or transferred tokens")
    if total <= 0:
        return Decimal(0), Decimal(0), Decimal(0)
    if total - quantity < DUST:
        traded, cost, external = (position["traded_qty"], position["traded_cost"],
                                  position["external_qty"])
        position["traded_qty"] = position["traded_cost"] = position["external_qty"] = Decimal(0)
        return traded, cost, external
    traded = quantity * position["traded_qty"] / total
    external = quantity - traded
    cost = (position["traded_cost"] * traded / position["traded_qty"]
            if position["traded_qty"] > 0 else Decimal(0))
    position["traded_qty"] -= traded
    position["traded_cost"] -= cost
    position["external_qty"] -= external
    return traded, cost, external


def analyze_ledger(data, address, network="solana", now=None, max_age_s=21600,
                   min_history_days=30, cycle_dust_fraction="0.001"):
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
    if start > now - min_history_days * DAY:
        raise ValueError(f"Need at least {min_history_days} days of history and earlier cost basis")
    rows = data.get("transactions")
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ValueError("Expected at most 50000 fully paginated transactions")
    dust_fraction = decimal(cycle_dust_fraction, "cycle_dust_fraction")
    positions, sales, cycles, expenses, activity, seen = {}, [], [], [], [], set()
    external_sales, censored_cost, deployed_cost, estimated = [], Decimal(0), Decimal(0), 0
    previous = start

    def token_of(row, key="token"):
        value = row.get(key)
        if not valid_address(value):
            raise ValueError("Invalid token mint address")
        return value

    def close_cycle(token, ts, censored=False):
        """End a round trip. A censored exit realised nothing observable, so it
        contributes no win or loss even though its earlier sales still count."""
        p = positions[token]
        if p["funded"] and p["sold_cost"] > 0 and not censored:
            cycles.append({"ts": ts, "opened": p["opened"], "token": token,
                           "pnl": p["pnl"], "cost": p["sold_cost"]})
        p["pnl"], p["sold_cost"], p["opened"] = Decimal(0), Decimal(0), ts
        # The crumb left behind is not a new round trip until it is bought into.
        p["peak_qty"], p["funded"] = p["traded_qty"], False
        if p["traded_qty"] <= 0 and p["external_qty"] <= 0:
            del positions[token]

    def sell_from(token, quantity, amount, fee, ts):
        p = positions.get(token)
        if p is None:
            raise ValueError("Sell exceeds known inventory; missing purchases or transferred tokens")
        traded, cost, external = remove(p, quantity, "Sell")
        # Proceeds follow the coins: the externally sourced share is not skill.
        traded_share = traded / quantity if quantity > 0 else Decimal(0)
        p["pnl"] += amount * traded_share - fee * traded_share - cost
        p["sold_cost"] += cost
        sales.append({"ts": ts, "token": token,
                      "pnl": amount * traded_share - fee * traded_share - cost, "cost": cost})
        if external > 0:
            external_share = Decimal(1) - traded_share
            external_sales.append({"ts": ts, "pnl": amount * external_share - fee * external_share})
        if exhausted(p, dust_fraction):
            close_cycle(token, ts)

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
        if side not in TRADE_SIDES:
            raise ValueError("Unsupported ledger row; the adapter must classify every transaction")
        if side == "fee":
            expenses.append((ts, fee))
            continue
        if side in ("buy", "sell"):
            token = token_of(row)
            qty = decimal(row.get("quantity"), "quantity")
            amount = decimal(row.get("notional_usd"), "notional_usd")
            if qty <= 0 or (side == "buy" and amount <= 0):
                raise ValueError("Trade requires positive quantity and a known buy cost")
            activity.append((ts, token))
            if side == "buy":
                p = positions.setdefault(token, new_position(ts))
                p["traded_qty"] += qty
                p["traded_cost"] += amount + fee
                p["peak_qty"] = max(p["peak_qty"], p["traded_qty"])
                p["funded"] = True
                deployed_cost += amount + fee
            else:
                sell_from(token, qty, amount, fee, ts)
        elif side in ("transfer_in", "transfer_out"):
            token = token_of(row)
            qty = decimal(row.get("quantity"), "quantity")
            if qty <= 0:
                raise ValueError("Transfer requires a positive quantity")
            if side == "transfer_in":
                # Arrives with no knowable basis; profit from it is tracked apart
                # from trading skill rather than crediting a zero-cost buy.
                positions.setdefault(token, new_position(ts))["external_qty"] += qty
            else:
                p = positions.get(token)
                if p is None:
                    raise ValueError("Transfer out exceeds known inventory")
                traded, cost, external = remove(p, qty, "Transfer out")
                censored_cost += cost
                if exhausted(p, dust_fraction):
                    close_cycle(token, ts, censored=cost > 0)
        elif side == "swap":
            out_token, in_token = token_of(row, "token_out"), token_of(row, "token_in")
            out_qty = decimal(row.get("quantity_out"), "quantity_out")
            in_qty = decimal(row.get("quantity_in"), "quantity_in")
            if out_qty <= 0 or in_qty <= 0 or out_token == in_token:
                raise ValueError("Swap requires two distinct tokens and positive quantities")
            source = positions.get(out_token)
            if source is None:
                raise ValueError("Swap exceeds known inventory; missing purchases or transfers")
            activity.append((ts, in_token))
            traded, cost, external = remove(source, out_qty, "Swap")
            # Basis carries across instead of realising at a price neither leg
            # supplies. Total profit stays exact; only the cycle count drops.
            share = traded / out_qty if out_qty > 0 else Decimal(0)
            carried_pnl, carried_sold = source["pnl"], source["sold_cost"]
            opened, source_funded = source["opened"], source["funded"]
            source["pnl"], source["sold_cost"] = Decimal(0), Decimal(0)
            if source["traded_qty"] <= 0 and source["external_qty"] <= 0:
                del positions[out_token]
            target = positions.setdefault(in_token, new_position(opened))
            target["traded_qty"] += in_qty * share
            target["traded_cost"] += cost + fee
            target["peak_qty"] = max(target["peak_qty"], target["traded_qty"])
            target["funded"] = target["funded"] or source_funded
            target["external_qty"] += in_qty * (Decimal(1) - share)
            target["pnl"] += carried_pnl
            target["sold_cost"] += carried_sold
            target["opened"] = min(target["opened"], opened)
            deployed_cost += fee
        else:
            legs = row.get("legs")
            amount = decimal(row.get("notional_usd"), "notional_usd")
            if not isinstance(legs, list) or not 2 <= len(legs) <= 50:
                raise ValueError("A batch sell needs between 2 and 50 legs")
            parsed = []
            for leg in legs:
                if not isinstance(leg, dict):
                    raise ValueError("Each batch leg must be an object")
                leg_token = token_of(leg)
                leg_qty = decimal(leg.get("quantity"), "quantity")
                if leg_qty <= 0 or leg_token not in positions:
                    raise ValueError("Batch leg exceeds known inventory")
                p = positions[leg_token]
                total = p["traded_qty"] + p["external_qty"]
                basis = (p["traded_cost"] * min(leg_qty, total) / p["traded_qty"]
                         if p["traded_qty"] > 0 else Decimal(0))
                parsed.append({"token": leg_token, "quantity": leg_qty, "basis": basis})
            # One transaction, one price for several tokens: the split between them
            # is an estimate, so it is counted and surfaced rather than hidden.
            estimated += 1
            weights = [leg["basis"] for leg in parsed]
            if sum(weights) <= 0:
                weights = [leg["quantity"] for leg in parsed]
            total_weight = sum(weights)
            for leg, weight in zip(parsed, weights):
                portion = weight / total_weight if total_weight > 0 else Decimal(0)
                activity.append((ts, leg["token"]))
                sell_from(leg["token"], leg["quantity"], amount * portion, fee * portion, ts)
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
        held = positions[token]["traded_qty"] + positions[token]["external_qty"]
        if abs(decimal(mark["quantity"], "mark quantity") - held) > DUST:
            raise ValueError("Inventory mark quantity does not reconcile")
        mark_map[token] = decimal(mark["value_usd"], "value_usd")
    if set(mark_map) != set(positions):
        raise ValueError("Missing valuation for open inventory; do not omit losing tokens")

    def traded_value(token):
        """The share of a mark backed by bought inventory; airdropped coins are
        worth something but are not evidence of trading."""
        p = positions[token]
        total = p["traded_qty"] + p["external_qty"]
        return mark_map[token] * p["traded_qty"] / total if total > 0 else Decimal(0)

    open_cost = sum((p["traded_cost"] for p in positions.values()), Decimal(0))
    open_pnl = sum((traded_value(t) - p["traded_cost"] for t, p in positions.items()), Decimal(0))
    # Winning open positions cannot cancel the penalty for open losers.
    open_loss = sum((min(Decimal(0), traded_value(t) - p["traded_cost"])
                     for t, p in positions.items()), Decimal(0))
    open_external_value = sum((mark_map[t] - traded_value(t) for t in positions), Decimal(0))
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
            "external_origin_pnl_usd": float(sum((s["pnl"] for s in external_sales
                                                  if s["ts"] >= cutoff), Decimal(0))),
        }
    return {"address": address, "network": network, "asof": end, "source": source,
            "coverage": quality, "accounting": "origin_split_moving_weighted_average_after_fees",
            "open_cost_usd": float(open_cost), "unrealized_pnl_usd": float(open_pnl),
            "open_loss_usd": float(open_loss), "open_tokens": len(positions),
            "open_external_value_usd": float(open_external_value),
            "censored_cost_usd": float(censored_cost),
            "censored_cost_fraction": ratio(censored_cost, deployed_cost) or 0.0,
            "estimated_allocation_rows": estimated,
            "external_origin_pnl_note": "Profit from transferred-in inventory, excluded from cost_roi and cycles",
            "windows": windows}


WALLET_FLAGS = ("insufficient_independent_sample", "profit_concentrated_in_one_token",
                "not_profitable_without_best_token", "realized_profit_does_not_cover_open_losses",
                "record_materially_censored", "allocation_estimated")
BLOCKING_FLAGS = ("insufficient_independent_sample", "realized_profit_does_not_cover_open_losses",
                  "record_materially_censored")


def rank_wallets(analyses, min_cycles=10, min_tokens=3, max_censored_fraction=.25):
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
        # Realising winners while holding losers raises cost_roi, which carries most
        # of the score. Without a hard gate a wallet that is net down outranks a
        # genuinely profitable one, so insolvency cannot be a cosmetic annotation.
        if m["realized_pnl_usd"] + item["open_loss_usd"] <= 0:
            flags.append("realized_profit_does_not_cover_open_losses")
        # Inventory moved out is an outcome nobody can observe. A little is normal
        # housekeeping; a lot means the record simply does not show what happened.
        if item["censored_cost_fraction"] > max_censored_fraction:
            flags.append("record_materially_censored")
        if item["estimated_allocation_rows"]:
            flags.append("allocation_estimated")
        confidence = (m["closed_cycles"] / (m["closed_cycles"] + 20)
                      * min(1, m["closed_tokens"] / 8) ** .5
                      * min(1, m["active_weeks"] / 6) ** .5)
        transform = lambda value: math.tanh((value or 0) / .2)
        edge = (.45 * transform(m["cost_roi"]) + .25 * transform(m["without_best_token_roi"])
                + .20 * transform(m["inventory_stressed_roi"]) + .10 * m["cycle_profit_balance"])
        # A penalty can only reduce a positive score, never improve a loser.
        concentration = 1 - .7 * (m["best_token_profit_share"] or 0)
        score = 100 * confidence * (edge * concentration if edge > 0 else edge)
        eligible = not any(f in BLOCKING_FLAGS for f in flags) and m["cost_roi"] is not None
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
