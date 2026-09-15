"""Turn raw Solana transactions into the bot's ledger rows, using balance deltas.

Balance deltas are used deliberately instead of a provider's parsed SWAP event:
they capture the wallet's actual net position change no matter which program,
aggregator or route produced it, and they cannot silently omit a leg. The cost
is that anything which is not a clean two-sided swap is reported as unsupported
rather than guessed at, which is what the ranking requires.
"""
from __future__ import annotations

from decimal import Decimal

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
            mint = balance["mint"]
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
    """-> (side, mint, quantity, notional_usd); notional_usd is positive.

    'skip' means the transaction cannot affect token cost accounting at all.
    'unsupported' means it could, but its cost basis is unknowable from deltas.
    """
    if row["failed"]:
        # A failed attempt still costs gas and belongs to the strategy's cost.
        return ("fee", None, None, None) if row["fee_sol"] > 0 else ("skip", None, None, None)
    traded = row["tokens"]
    if not traded:
        # Quote-asset movement only: deposits, withdrawals, unrelated activity.
        # None of it enters token cost accounting, so it is skipped, not rejected.
        return "skip", None, None, None
    if len(traded) > 1:
        # Token-for-token swaps need a USD price for a research asset, which
        # quote-leg deltas cannot supply. Reported rather than guessed.
        return "unsupported", "multi_token_transaction", None, None
    mint, quantity = next(iter(traded.items()))
    if quantity > 0 and quote_value < 0:
        return "buy", mint, quantity, -quote_value
    if quantity < 0 and quote_value > 0:
        return "sell", mint, -quantity, quote_value
    # Tokens moved with no matching quote leg: transfer in/out, airdrop or LP.
    return "unsupported", "transfer_or_airdrop", None, None


def ledger_rows(entries, address, price_usd):
    """Normalized ledger rows, plus every transaction that could not be used.

    `price_usd(mint, ts)` returns the USD price of one unit of a quote asset.
    """
    rows, rejected = [], []
    for row in sorted((normalize(e, address) for e in entries),
                      key=lambda r: (r["ts"], r["slot"], r["order"])):
        side, mint, quantity, notional = classify(row, quote_usd(row, price_usd))
        if side == "skip":
            continue
        if side == "unsupported":
            rejected.append({"signature": row["signature"], "ts": row["ts"], "reason": mint})
            continue
        fee_usd = row["fee_sol"] * price_usd(SOL_MINT, row["ts"])
        if side == "fee":
            rows.append({"id": row["signature"] + ":fee", "ts": row["ts"], "side": "fee",
                         "fee_usd": str(fee_usd)})
            continue
        rows.append({"id": row["signature"] + ":0", "ts": row["ts"], "side": side,
                     "token": mint, "quantity": str(quantity),
                     "notional_usd": str(notional), "fee_usd": str(fee_usd)})
    return rows, rejected
