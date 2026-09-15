"""Ledger reconstruction from Solana balance deltas. Offline: no RPC, no network.

Fixtures follow the documented getTransactionsForAddress `transactionDetails:
"full"` shape. They verify the reconstruction rules, not Helius availability.
"""
from decimal import Decimal
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.reconstruct import (SOL_MINT, classify, ledger_rows, normalize,
                                  token_decimals)
from twobots.wallets import analyze_ledger

WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
OTHER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
POPCAT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
DAY = 86400
SOL_USD = Decimal("150")


def price_usd(mint, ts):
    return Decimal(1) if mint == USDC else SOL_USD


def balance(index, mint, owner, amount, decimals=6):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def entry(sig, ts, *, slot=1, order=0, err=None, fee=5000, lamports=None,
          keys=None, pre=(), post=()):
    keys = keys if keys is not None else [WALLET, "11111111111111111111111111111111"]
    # Default: the wallet's SOL moves by its fee and nothing else.
    lamports = lamports if lamports is not None else (10 ** 9, 10 ** 9 - fee)
    return {"blockTime": ts, "slot": slot, "transactionIndex": order,
            "transaction": {"signatures": [sig],
                            "message": {"accountKeys": [{"pubkey": k} for k in keys]}},
            "meta": {"err": err, "fee": fee,
                     "preBalances": [lamports[0], 0], "postBalances": [lamports[1], 0],
                     "preTokenBalances": list(pre), "postTokenBalances": list(post)}}


def usdc_swap(sig, ts, token, token_qty, usdc_qty, **kw):
    """usdc_qty negative means USDC left the wallet, i.e. a buy."""
    return entry(sig, ts,
                 pre=[balance(1, USDC, WALLET, 1_000_000_000), balance(2, token, WALLET, 0, 9)],
                 post=[balance(1, USDC, WALLET, 1_000_000_000 + int(usdc_qty * 10 ** 6)),
                       balance(2, token, WALLET, int(token_qty * 10 ** 9), 9)], **kw)


def test_usdc_buy_uses_the_quote_leg_for_notional():
    rows, rejected = ledger_rows([usdc_swap("s1", 1_700_000_000, BONK, 1000, -250)],
                                 WALLET, price_usd)
    assert rejected == []
    assert len(rows) == 1
    row = rows[0]
    assert row["side"] == "buy" and row["token"] == BONK
    assert Decimal(row["quantity"]) == 1000
    assert Decimal(row["notional_usd"]) == 250


def test_usdc_sell_is_the_mirror_case():
    rows, _ = ledger_rows([usdc_swap("s2", 1_700_000_000, BONK, -1000, 400)], WALLET, price_usd)
    assert rows[0]["side"] == "sell"
    assert Decimal(rows[0]["quantity"]) == 1000
    assert Decimal(rows[0]["notional_usd"]) == 400


def test_sol_leg_is_priced_and_the_fee_is_added_back():
    """postBalances already has the fee deducted; leaving it in misprices the swap."""
    spent, fee = 2 * 10 ** 9, 5000
    e = entry("s3", 1_700_000_000, fee=fee, lamports=(10 * 10 ** 9, 10 * 10 ** 9 - spent - fee),
              pre=[balance(2, BONK, WALLET, 0, 9)],
              post=[balance(2, BONK, WALLET, 500 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "buy"
    assert Decimal(rows[0]["notional_usd"]) == 2 * SOL_USD
    assert Decimal(rows[0]["fee_usd"]) == Decimal(fee) / 10 ** 9 * SOL_USD


def test_wrapped_sol_counts_as_the_same_quote_asset():
    e = entry("s4", 1_700_000_000, fee=0,
              pre=[balance(1, SOL_MINT, WALLET, 3 * 10 ** 9, 9), balance(2, BONK, WALLET, 0, 9)],
              post=[balance(1, SOL_MINT, WALLET, 10 ** 9, 9),
                    balance(2, BONK, WALLET, 500 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert Decimal(rows[0]["notional_usd"]) == 2 * SOL_USD


def test_failed_transaction_becomes_a_standalone_fee():
    e = entry("s5", 1_700_000_000, err={"InstructionError": [0, "Custom"]}, fee=7000,
              lamports=(10 ** 9, 10 ** 9 - 7000))
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "fee"
    assert Decimal(rows[0]["fee_usd"]) == Decimal(7000) / 10 ** 9 * SOL_USD


def test_quote_asset_deposit_is_skipped_not_rejected():
    """Everyone funds their wallet. Cash movements never touch token cost accounting."""
    e = entry("s6", 1_700_000_000, fee=5000,
              pre=[balance(1, USDC, WALLET, 0)],
              post=[balance(1, USDC, WALLET, 5_000_000_000)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rows == [] and rejected == []


def test_token_arriving_without_payment_is_rejected():
    e = entry("s7", 1_700_000_000, fee=0,
              pre=[balance(2, BONK, WALLET, 0, 9)],
              post=[balance(2, BONK, WALLET, 10_000 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rows == []
    assert rejected[0]["reason"] == "transfer_or_airdrop"


def test_token_for_token_swap_is_rejected():
    e = entry("s8", 1_700_000_000, fee=0,
              pre=[balance(2, BONK, WALLET, 1000 * 10 ** 9, 9), balance(3, WIF, WALLET, 0, 9)],
              post=[balance(2, BONK, WALLET, 0, 9), balance(3, WIF, WALLET, 5 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rows == []
    assert rejected[0]["reason"] == "multi_token_transaction"


def test_other_owners_balances_are_ignored():
    """A counterparty's token account appears in the same transaction metadata."""
    e = usdc_swap("s9", 1_700_000_000, BONK, 1000, -250)
    e["meta"]["postTokenBalances"].append(balance(7, BONK, OTHER, 999_999 * 10 ** 9, 9))
    rows, _ = ledger_rows([e], WALLET, price_usd)
    assert Decimal(rows[0]["quantity"]) == 1000


def test_fee_is_only_charged_to_the_fee_payer():
    e = usdc_swap("s10", 1_700_000_000, BONK, 1000, -250, fee=9000,
                  keys=[OTHER, WALLET], lamports=(10 ** 9, 10 ** 9 - 9000))
    row = normalize(e, WALLET)
    assert row["fee_sol"] == 0
    assert Decimal(ledger_rows([e], WALLET, price_usd)[0][0]["fee_usd"]) == 0


def test_same_second_transactions_keep_execution_order():
    ts = 1_700_000_000
    late = usdc_swap("late", ts, BONK, -1000, 400, slot=9, order=1)
    early = usdc_swap("early", ts, BONK, 1000, -250, slot=9, order=0)
    rows, _ = ledger_rows([late, early], WALLET, price_usd)
    assert [r["id"] for r in rows] == ["early:0", "late:0"]


def test_decimals_are_read_from_the_chain():
    e = usdc_swap("s11", 1_700_000_000, BONK, 1000, -250)
    assert token_decimals([e], WALLET)[BONK] == 9
    assert token_decimals([e], WALLET)[USDC] == 6


def test_dust_balance_changes_do_not_invent_a_trade():
    e = entry("s12", 1_700_000_000, fee=0,
              pre=[balance(2, BONK, WALLET, 1, 30)], post=[balance(2, BONK, WALLET, 2, 30)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rows == [] and rejected == []


def test_reconstructed_ledger_is_accepted_by_the_ranking():
    """The adapter's output has to survive the bot's own schema and accounting."""
    import time
    now = time.time()
    start = now - 120 * DAY
    entries, tokens = [], [BONK, WIF, POPCAT]
    for i in range(15):
        ts = int(now - 85 * DAY + i * 2 * DAY)
        token = tokens[i % 3]
        gain = 400 if i % 3 == 0 else -100
        entries.append(usdc_swap(f"b{i}", ts, token, 1000, -500, slot=100 + i))
        entries.append(usdc_swap(f"s{i}", ts + 3600, token, -1000, 500 + gain, slot=200 + i))
    rows, rejected = ledger_rows(entries, WALLET, price_usd)
    assert rejected == []
    data = {"schema_version": 1, "currency": "USD", "network": "solana", "address": WALLET,
            "source": "test", "history_start": int(start), "asof": int(now),
            "quality": {k: True for k in ("complete", "initial_inventory_empty", "all_protocols",
                                          "fees_included", "transfers_included",
                                          "failed_transactions_included")},
            "transactions": rows, "marks": []}
    result = analyze_ledger(data, WALLET, "solana", now)
    m = result["windows"]["90"]
    assert m["closed_cycles"] == 15
    assert m["closed_tokens"] == 3
    assert Decimal(str(m["realized_pnl_usd"])) == 5 * 400 - 10 * 100 - Decimal(30 * 5000) / 10 ** 9 * SOL_USD


def test_open_inventory_marks_reconcile_with_the_bots_replay():
    """A mark quantity that does not match the replayed position rejects the wallet."""
    import time
    from adapters.ledger import inventory
    now = time.time()
    start = now - 120 * DAY
    entries = []
    for i in range(12):
        ts = int(start + 5 * DAY + i * 2 * DAY)
        token = [BONK, WIF, POPCAT][i % 3]
        entries.append(usdc_swap(f"b{i}", ts, token, 1000, -500, slot=100 + i))
        entries.append(usdc_swap(f"s{i}", ts + 3600, token, -1000, 900, slot=200 + i))
    entries.append(usdc_swap("open", int(now - 10 * DAY), BONK, 750, -500, slot=900))
    rows, _ = ledger_rows(entries, WALLET, price_usd)
    held = inventory(rows)
    assert held == {BONK: Decimal(750)}
    data = {"schema_version": 1, "currency": "USD", "network": "solana", "address": WALLET,
            "source": "test", "history_start": int(start), "asof": int(now),
            "quality": {k: True for k in ("complete", "initial_inventory_empty", "all_protocols",
                                          "fees_included", "transfers_included",
                                          "failed_transactions_included")},
            "transactions": rows,
            "marks": [{"token": BONK, "quantity": str(held[BONK]), "value_usd": "120",
                       "asof": int(now)}]}
    result = analyze_ledger(data, WALLET, "solana", now)
    assert result["open_tokens"] == 1
    assert result["open_loss_usd"] < 0


def test_account_rent_in_sol_does_not_reject_a_usdc_swap():
    """Opening a token account costs SOL rent. Nearly every real buy looks like this."""
    rent = 2_039_280
    e = entry("s13", 1_700_000_000, fee=5000,
              lamports=(10 ** 9, 10 ** 9 - rent - 5000),
              pre=[balance(1, USDC, WALLET, 1_000_000_000), balance(2, BONK, WALLET, 0, 9)],
              post=[balance(1, USDC, WALLET, 750_000_000),
                    balance(2, BONK, WALLET, 500 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "buy"
    # Both legs are charged: 250 USDC plus the rent, valued in USD.
    assert Decimal(rows[0]["notional_usd"]) == 250 + Decimal(rent) / 10 ** 9 * SOL_USD


def test_activity_feed_output_is_accepted_by_the_copy_trader(tmp_path):
    """Adapter #2's output has to survive the bot's own feed validation."""
    import json
    import time
    from adapters.activity import fills_from_entries, leaders_from_store, write_feed
    from twobots.follow import ActivityFeed

    now = time.time()
    entries = [usdc_swap("f1", int(now - 20), BONK, 1000, -250, slot=10),
               usdc_swap("f2", int(now - 10), WIF, -50, 900, slot=11),
               # A deposit and an airdrop must not appear as leader fills.
               entry("f3", int(now - 15), pre=[balance(1, USDC, WALLET, 0)],
                     post=[balance(1, USDC, WALLET, 5_000_000_000)]),
               entry("f4", int(now - 12), fee=0, pre=[balance(2, POPCAT, WALLET, 0, 9)],
                     post=[balance(2, POPCAT, WALLET, 10 ** 12, 9)])]
    fills = fills_from_entries(entries, WALLET, price_usd)
    assert [(f["side"], f["token"]) for f in fills] == [("buy", BONK), ("sell", WIF)]

    write_feed(tmp_path, WALLET, fills, now)
    cfg = {"scanner": {"network": "solana"},
           "follow": {"source": "local", "activity_dir": str(tmp_path),
                      "max_activity_mb": 4, "max_feed_age_s": 300}}
    parsed = ActivityFeed(cfg, None).fills(WALLET, now)
    assert [f["id"] for f in parsed] == [f["id"] for f in fills]
    assert parsed[0]["notional_usd"] == 250.0

    # The leader list is read straight out of the bot's store, never re-ranked here.
    import sqlite3
    leaders = json.dumps({"at": now, "addresses": [WALLET]})
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    db.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    db.execute("INSERT INTO kv VALUES('copy:leaders', ?)", (leaders,))
    db.commit()
    db.close()
    assert leaders_from_store(tmp_path) == [WALLET]


def test_activity_feed_write_is_atomic(tmp_path):
    """A half-written feed would be read as a schema error and drop every signal."""
    from adapters.activity import write_feed
    path = write_feed(tmp_path, WALLET, [], 1_700_000_000)
    assert path.exists()
    assert not list(tmp_path.glob("*.part"))


class FakeHelius:
    def __init__(self, entries):
        self.entries, self.calls = entries, 1

    def transactions(self, address, **kwargs):
        return iter(self.entries)


class FakeSolPrice:
    def load(self, *args):
        return 0

    def at(self, ts):
        return SOL_USD


def clean_history(now, count=15):
    entries = []
    for i in range(count):
        ts = int(now - 85 * DAY + i * 2 * DAY)
        token = [BONK, WIF, POPCAT][i % 3]
        entries.append(usdc_swap(f"b{i}", ts, token, 1000, -500, slot=100 + i))
        entries.append(usdc_swap(f"s{i}", ts + 3600, token, -1000, 900, slot=200 + i))
    entries.append(entry("genesis", int(now - 120 * DAY),
                         pre=[balance(1, USDC, WALLET, 0)],
                         post=[balance(1, USDC, WALLET, 50_000_000_000)]))
    return entries


def test_build_reports_an_unusable_wallet_instead_of_raising():
    """One unsolicited airdrop is enough, and the report has to say so."""
    import time
    from adapters.ledger import build
    now = time.time()
    entries = clean_history(now)
    entries.append(entry("spam", int(now - 30 * DAY), fee=0,
                         pre=[balance(5, POPCAT, WALLET, 0, 9)],
                         post=[balance(5, POPCAT, WALLET, 10 ** 15, 9)]))
    ledger, report = build(WALLET, FakeHelius(entries), FakeSolPrice(), now=now, quote_marks=False)
    assert ledger is None
    assert report["usable"] is False
    assert report["blocked_by"] == "unreconstructable_cost_basis"
    assert report["reasons"] == {"transfer_or_airdrop": 1}
    assert report["trade_rows"] == 30


def test_build_produces_a_ledger_the_ranking_accepts():
    import time
    from adapters.ledger import build
    now = time.time()
    ledger, report = build(WALLET, FakeHelius(clean_history(now)), FakeSolPrice(),
                           now=now, quote_marks=False)
    assert report["usable"] is True and report["unreconstructable"] == 0
    result = analyze_ledger(ledger, WALLET, "solana", now)
    assert result["windows"]["90"]["closed_cycles"] == 15


def test_build_reports_short_history_without_spending_on_marks():
    import time
    from adapters.ledger import build
    now = time.time()
    entries = [usdc_swap("b0", int(now - 10 * DAY), BONK, 1000, -500)]
    ledger, report = build(WALLET, FakeHelius(entries), FakeSolPrice(), now=now, quote_marks=False)
    assert ledger is None
    assert report["blocked_by"] == "history_shorter_than_90_days"
    assert report["history_days"] == 10


@pytest.mark.parametrize("field", ["signatures", "blockTime"])
def test_malformed_entries_raise_rather_than_silently_drop(field):
    e = usdc_swap("s14", 1_700_000_000, BONK, 1000, -250)
    if field == "signatures":
        e["transaction"]["signatures"] = []
    else:
        del e["blockTime"]
    with pytest.raises((ValueError, KeyError)):
        ledger_rows([e], WALLET, price_usd)
