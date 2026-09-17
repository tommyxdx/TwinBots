"""Turn raw Solana transactions into the bot's ledger rows, using balance deltas.

Balance deltas are used deliberately instead of a provider's parsed SWAP event:
they capture the wallet's actual net position change no matter which program,
aggregator or route produced it, and they cannot silently omit a leg. The cost
is that anything which is not a clean two-sided swap is reported as unsupported
rather than guessed at, which is what the ranking requires.
"""
from __future__ import annotations

from decimal import Decimal
import sys

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = Decimal(10) ** 9
# USD-pegged mints are priced at 1.0; depeg is not modelled.
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
DUST = Decimal("1e-12")


def account_keys(entry):
    message = (entry.get("transaction") or {}).get("message") or {}
    return [k.get("pubkey") if isinstance(k, dict) else k
            for k in (message.get("accountKeys") or [])]


def signature(entry):
    if entry.get("signature"):
        return entry["signature"]
    signatures = (entry.get("transaction") or {}).get("signatures") or []
    if not signatures:
        raise ValueError("Transaction is missing its signature")
    return signatures[0]


def token_amount(balance):
    ui = balance.get("uiTokenAmount") or {}
    return Decimal(str(ui["amount"])) / (Decimal(10) ** int(ui["decimals"]))


def token_deltas(entry, address):
    """Net change per mint across every token account owned by `address`."""
    meta = entry.get("meta") or {}
    totals = {}
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for balance in meta.get(key) or []:
            if balance.get("owner") != address:
                continue
            # The same few mints repeat across an entire history; one copy each.
            mint = sys.intern(balance["mint"])
            totals[mint] = totals.get(mint, Decimal(0)) + sign * token_amount(balance)
    return {mint: amount for mint, amount in totals.items() if abs(amount) > DUST}


def token_decimals(entries, address):
    """Mint decimals as reported by the chain, needed to quote held inventory."""
    found = {}
    for entry in entries:
        meta = entry.get("meta") or {}
        for key in ("preTokenBalances", "postTokenBalances"):
            for balance in meta.get(key) or []:
                if balance.get("owner") == address:
                    found[balance["mint"]] = int((balance.get("uiTokenAmount") or {})["decimals"])
    return found


def native_delta(entry, address):
    """SOL change for `address`, with its own network fee added back.

    The fee is already deducted from postBalances, so leaving it in would make a
    swap look marginally cheaper or dearer than the amount actually exchanged.
    It is returned separately and charged once, by the caller.
    """
    meta = entry.get("meta") or {}
    keys = account_keys(entry)
    if address not in keys:
        return Decimal(0), 0
    index = keys.index(address)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if index >= len(pre) or index >= len(post):
        return Decimal(0), 0
    # Only the fee payer, which is always the first account key, pays the fee.
    fee = int(meta.get("fee") or 0) if index == 0 else 0
    return (Decimal(post[index] - pre[index]) + fee) / LAMPORTS, fee


def normalize(entry, address):
    """One transaction reduced to the wallet's own net effect."""
    tokens = token_deltas(entry, address)
    native, fee_lamports = native_delta(entry, address)
    # Wrapped SOL is the same asset; a route may leave the wallet holding either.
    legs = {SOL_MINT: native + tokens.pop(SOL_MINT, Decimal(0))}
    for mint in STABLE_MINTS:
        legs[mint] = tokens.pop(mint, Decimal(0))
    return {"ts": int(entry["blockTime"]), "slot": int(entry.get("slot") or 0),
            "order": int(entry.get("transactionIndex") or 0), "signature": signature(entry),
            "failed": (entry.get("meta") or {}).get("err") is not None,
            "fee_sol": Decimal(fee_lamports) / LAMPORTS,
            "quote_legs": {m: a for m, a in legs.items() if abs(a) > DUST}, "tokens": tokens}


def quote_usd(row, price_usd):
    """Signed USD paid or received across every quote asset in one transaction.

    Legs are summed rather than requiring a single quote asset: paying in USDC
    while the same transaction moves a little SOL for account rent is ordinary,
    and rejecting it would disqualify nearly every real wallet.
    """
    return sum((amount * price_usd(mint, row["ts"]) for mint, amount in row["quote_legs"].items()),
               Decimal(0))


def classify(row, quote_value):
    """-> (side, payload); every side the ledger schema understands is reachable.

    Nothing about a wallet's ordinary life is rejected outright any more: what
    the deltas cannot price is recorded in a form the accounting can hold open
    without inventing a cost basis for it.
    """
    if row["failed"]:
        # A failed attempt still costs gas and belongs to the strategy's cost.
        return ("fee", {}) if row["fee_sol"] > 0 else ("skip", {})
    traded = row["tokens"]
    if not traded:
        # Quote-asset movement only: deposits, withdrawals, unrelated activity.
        # None of it enters token cost accounting, so it is skipped, not rejected.
        return "skip", {}
    incoming = {m: q for m, q in traded.items() if q > 0}
    outgoing = {m: -q for m, q in traded.items() if q < 0}
    priced = abs(quote_value) > DUST
    if len(traded) == 1:
        mint, quantity = next(iter(traded.items()))
        if quantity > 0:
            if quote_value < 0:
                return "buy", {"token": mint, "quantity": quantity, "notional_usd": -quote_value}
            # Arrived without payment: airdrop, or a transfer from another wallet.
            return "transfer_in", {"token": mint, "quantity": quantity}
        if quote_value > 0:
            return "sell", {"token": mint, "quantity": -quantity, "notional_usd": quote_value}
        return "transfer_out", {"token": mint, "quantity": -quantity}
    if len(incoming) == 1 and len(outgoing) == 1 and not priced:
        # Token for token. Basis carries across, so no price is needed.
        (in_mint, in_qty), (out_mint, out_qty) = next(iter(incoming.items())), next(iter(outgoing.items()))
        return "swap", {"token_out": out_mint, "quantity_out": out_qty,
                        "token_in": in_mint, "quantity_in": in_qty}
    if outgoing and not incoming and quote_value > 0:
        # Several positions closed at one price; the split between them is an estimate.
        return "batch_sell", {"legs": [{"token": m, "quantity": q} for m, q in sorted(outgoing.items())],
                              "notional_usd": quote_value}
    # Several tokens acquired at one price, or a mixed multi-leg transaction:
    # there is no defensible key to allocate cost across the incoming legs.
    return "unsupported", {"reason": "unallocatable_multi_token_transaction"}


def ledger_rows(entries, address, price_usd):
    """Convenience wrapper: normalize raw transactions, then reduce them."""
    rows, rejected, _ = rows_from_normalized([normalize(e, address) for e in entries], price_usd)
    return rows, rejected


def rows_from_normalized(normalized, price_usd):
    """Ledger rows from already-normalized transactions, plus what was unusable.

    Taking normalized rows rather than raw ones lets a caller drop each
    transaction's JSON as it streams: the raw form is around 13 KB and a
    normalized row a fraction of that, which at a long history is the difference
    between a gigabyte and a few tens of megabytes.

    `price_usd(mint, ts)` returns the USD price of one unit of a quote asset.

    Consumes `normalized`: it is sorted in place and emptied as it is read, so
    the output list grows while the input shrinks instead of both sitting in
    memory at once. At sixty thousand transactions that is the difference
    between one peak and two on a machine that has no swap to absorb either.
    """
    rows, rejected, skipped = [], [], 0
    normalized.sort(key=lambda r: (r["ts"], r["slot"], r["order"]))
    for index, row in enumerate(normalized):
        normalized[index] = None
        side, payload = classify(row, quote_usd(row, price_usd))
        if side == "skip":
            # Nothing the wallet holds as a research asset moved. Ordinary cash
            # movement looks like this — and so does a perp, lending or LP
            # position, whose economics never touch a token balance here.
            skipped += 1
            continue
        if side == "unsupported":
            rejected.append({"signature": row["signature"], "ts": row["ts"],
                             "reason": payload["reason"]})
            continue
        entry = {"id": row["signature"] + (":fee" if side == "fee" else ":0"),
                 "ts": row["ts"], "side": side,
                 "fee_usd": str(row["fee_sol"] * price_usd(SOL_MINT, row["ts"]))}
        for key, value in payload.items():
            entry[key] = ([{"token": leg["token"], "quantity": str(leg["quantity"])}
                           for leg in value] if key == "legs" else str(value))
        rows.append(entry)
    return rows, rejected, skipped
