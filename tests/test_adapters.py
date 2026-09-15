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


def test_token_arriving_without_payment_becomes_a_transfer_in():
    """An unsolicited airdrop is ordinary. It gets no cost basis, not a rejection."""
    e = entry("s7", 1_700_000_000, fee=0,
              pre=[balance(2, BONK, WALLET, 0, 9)],
              post=[balance(2, BONK, WALLET, 10_000 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "transfer_in"
    assert Decimal(rows[0]["quantity"]) == 10_000
    assert "notional_usd" not in rows[0]


def test_token_leaving_without_payment_becomes_a_transfer_out():
    e = entry("s7b", 1_700_000_000, fee=0,
              pre=[balance(2, BONK, WALLET, 500 * 10 ** 9, 9)],
              post=[balance(2, BONK, WALLET, 0, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "transfer_out"
    assert Decimal(rows[0]["quantity"]) == 500


def test_token_for_token_swap_carries_basis_instead_of_rejecting():
    e = entry("s8", 1_700_000_000, fee=0,
              pre=[balance(2, BONK, WALLET, 1000 * 10 ** 9, 9), balance(3, WIF, WALLET, 0, 9)],
              post=[balance(2, BONK, WALLET, 0, 9), balance(3, WIF, WALLET, 5 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "swap"
    assert rows[0]["token_out"] == BONK and Decimal(rows[0]["quantity_out"]) == 1000
    assert rows[0]["token_in"] == WIF and Decimal(rows[0]["quantity_in"]) == 5


def test_several_positions_closed_at_one_price_become_a_batch_sell():
    e = entry("s8b", 1_700_000_000, fee=0,
              pre=[balance(1, USDC, WALLET, 0), balance(2, BONK, WALLET, 1000 * 10 ** 9, 9),
                   balance(3, WIF, WALLET, 5 * 10 ** 9, 9)],
              post=[balance(1, USDC, WALLET, 900_000_000), balance(2, BONK, WALLET, 0, 9),
                    balance(3, WIF, WALLET, 0, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rejected == []
    assert rows[0]["side"] == "batch_sell"
    assert Decimal(rows[0]["notional_usd"]) == 900
    assert sorted(leg["token"] for leg in rows[0]["legs"]) == sorted([BONK, WIF])


def test_several_tokens_bought_at_one_price_stay_unsupported():
    """Cost has no defensible allocation key across incoming legs."""
    e = entry("s8c", 1_700_000_000, fee=0,
              pre=[balance(1, USDC, WALLET, 900_000_000), balance(2, BONK, WALLET, 0, 9),
                   balance(3, WIF, WALLET, 0, 9)],
              post=[balance(1, USDC, WALLET, 0), balance(2, BONK, WALLET, 1000 * 10 ** 9, 9),
                    balance(3, WIF, WALLET, 5 * 10 ** 9, 9)])
    rows, rejected = ledger_rows([e], WALLET, price_usd)
    assert rows == []
    assert rejected[0]["reason"] == "unallocatable_multi_token_transaction"


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


def test_price_loader_never_asks_for_an_unpublished_archive(tmp_path, monkeypatch):
    """Binance publishes a month's file after the month ends and a day's after the
    day ends. Requesting either at the live edge 404s, which used to fail every
    ledger build outright."""
    import time
    from datetime import datetime, timezone
    from adapters.prices import SolPrice
    now = time.time()
    today = datetime.fromtimestamp(now, timezone.utc).date()
    prices, asked = SolPrice(tmp_path), []

    def archive(kind, key):
        asked.append((kind, key))
        return {int(now - 5 * DAY): Decimal("100")}

    monkeypatch.setattr(prices, "_archive", archive)
    monkeypatch.setattr(prices, "_today", lambda end: {int(now - 30): Decimal("101")})
    prices.load(now - 70 * DAY, now)

    months = [k for kind, k in asked if kind == "monthly"]
    days = [k for kind, k in asked if kind == "daily"]
    assert today.strftime("%Y-%m") not in months, "the running month has no monthly file"
    assert today.isoformat() not in days, "today has no daily file either"
    assert days, "the running month still has to be covered day by day"
    assert all(d < today.isoformat() for d in days)
    # Today came from the REST tier, so a transaction a minute ago is priceable.
    assert prices.at(int(now - 10)) == Decimal("101")


def test_a_missing_archive_is_a_hole_not_a_crash(tmp_path, monkeypatch):
    from adapters.prices import SolPrice
    import time
    now = time.time()
    prices = SolPrice(tmp_path)
    monkeypatch.setattr(prices, "_archive", lambda kind, key: None)
    monkeypatch.setattr(prices, "_today", lambda end: {int(now - 30): Decimal("101")})
    prices.load(now - 70 * DAY, now)
    assert prices.missing, "the absent spans are reported"
    assert prices.at(int(now - 10)) == Decimal("101")
    # But a transaction inside the hole is refused rather than priced off a stale bar.
    with pytest.raises(ValueError, match="gap|at or before"):
        prices.at(int(now - 60 * DAY))


def test_a_sample_stops_at_the_page_cap_without_calling_it_a_failure():
    """Any active wallet has more than one page, and the shape sample only wants
    one. Treating 'there is more' as an error rejected every real trader at the
    first step."""
    from adapters.helius import Helius, TooMuchHistory
    client = Helius.__new__(Helius)
    client.calls = 0
    pages = [{"data": [{"blockTime": 1, "signature": f"s{i}"}], "paginationToken": f"t{i}"}
             for i in range(5)]
    calls = {"n": 0}

    def rpc(method, params):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    client.rpc = rpc
    got = list(client.transactions("addr", max_pages=1, partial_ok=True))
    assert len(got) == 1 and calls["n"] == 1, "one page requested, one page returned"

    calls["n"] = 0
    with pytest.raises(TooMuchHistory):
        # A caller that needs the whole history still has to hear about the cap.
        list(client.transactions("addr", max_pages=2))


class FakeHelius:
    def __init__(self, entries, size=None):
        self.entries, self.calls = entries, 1
        self.size = size or {"transactions": len(entries), "complete": True, "oldest": None}
        self.walked = False

    def history_size(self, address, **kwargs):
        return self.size

    def transactions(self, address, max_pages=400, **kwargs):
        if max_pages == 1:          # the cheap shape sample, not the full backfill
            return iter(self.entries[-3:])
        self.walked = True
        return iter(self.entries)


def test_a_market_maker_is_priced_out_before_its_history_is_bought():
    """400 pages spent and thrown away at the cap is the whole budget for nothing."""
    import time
    from adapters.ledger import build
    now = time.time()
    entries = clean_history(now)
    # 1000 sampled transactions inside two hours: roughly 12k a day.
    busy = FakeHelius(entries, {"transactions": 1000, "complete": False,
                                "oldest": now - 2 * 3600})
    ledger, report = build(WALLET, busy, FakeSolPrice(), now=now, quote_marks=False)
    assert ledger is None
    assert report["blocked_by"] == "history_exceeds_page_budget"
    assert report["transactions_per_day"] > 10_000
    assert busy.walked is False, "the full history must never be fetched"


def test_a_distributor_is_refused_on_one_sample_page():
    """Observed on live discovery: 3,799 buys, 161 sells, 3,415 forwarded out.

    The tokens leave before they are sold, so whatever happened to them happened
    at another address. Reconstructing the whole history cannot change that.
    """
    import time
    from adapters.ledger import build
    now = time.time()
    forwards = []
    for i in range(30):
        ts = int(now - 20 * DAY + i * 3600)
        forwards.append(usdc_swap(f"fb{i}", ts, BONK, 1000, -500, slot=900 + i))
        forwards.append(entry(f"fo{i}", ts + 60, fee=0, slot=950 + i,
                              pre=[balance(2, BONK, WALLET, 1000 * 10 ** 9, 9)],
                              post=[balance(2, BONK, WALLET, 0, 9)]))
    client = FakeHelius(clean_history(now) + forwards)
    original = client.entries

    def transactions(address, max_pages=400, **kwargs):
        if max_pages == 1:          # the recent page is all buy-and-forward
            return iter(forwards)
        client.walked = True
        return iter(original)

    client.transactions = transactions
    ledger, report = build(WALLET, client, FakeSolPrice(), now=now, quote_marks=False)
    assert ledger is None
    assert report["blocked_by"] == "buys_and_forwards_rather_than_trades"
    assert report["recent_mix"]["transfer_out"] > 0
    assert client.walked is False, "the full history is never bought for a distributor"


def test_a_trader_who_also_moves_a_position_out_is_not_mistaken_for_one():
    """Some housekeeping is normal; the filter only catches an address that
    essentially never sells."""
    import time
    from adapters.ledger import build
    now = time.time()
    mixed = []
    for i in range(30):
        ts = int(now - 20 * DAY + i * 3600)
        mixed.append(usdc_swap(f"mb{i}", ts, BONK, 1000, -500, slot=900 + i))
        if i % 5 == 0:
            mixed.append(entry(f"mo{i}", ts + 60, fee=0, slot=950 + i,
                               pre=[balance(2, BONK, WALLET, 1000 * 10 ** 9, 9)],
                               post=[balance(2, BONK, WALLET, 0, 9)]))
        else:
            mixed.append(usdc_swap(f"ms{i}", ts + 60, BONK, -1000, 600, slot=950 + i))
    client = FakeHelius(clean_history(now))
    original = client.entries

    def transactions(address, max_pages=400, **kwargs):
        if max_pages == 1:
            return iter(mixed)
        client.walked = True
        return iter(original)

    client.transactions = transactions
    ledger, report = build(WALLET, client, FakeSolPrice(), now=now, quote_marks=False)
    assert report["usable"] is True
    assert client.walked is True


def test_an_ordinary_wallet_passes_the_size_probe():
    import time
    from adapters.ledger import build
    now = time.time()
    entries = clean_history(now)
    steady = FakeHelius(entries, {"transactions": 1000, "complete": False,
                                  "oldest": now - 60 * DAY})
    ledger, report = build(WALLET, steady, FakeSolPrice(), now=now, quote_marks=False)
    assert report["usable"] is True
    assert steady.walked is True


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


def test_an_airdrop_no_longer_blocks_the_wallet():
    """Spam arrives unasked. It must not cost a wallet its eligibility."""
    import time
    from adapters.ledger import build
    now = time.time()
    entries = clean_history(now)
    entries.append(entry("spam", int(now - 30 * DAY), fee=0,
                         pre=[balance(5, POPCAT, WALLET, 0, 9)],
                         post=[balance(5, POPCAT, WALLET, 10 ** 15, 9)]))
    ledger, report = build(WALLET, FakeHelius(entries), FakeSolPrice(), now=now, quote_marks=False)
    assert report["usable"] is True
    assert report["rows_by_side"]["transfer_in"] == 1
    # The airdrop is open inventory, marked at zero because nothing was quoted.
    assert [m["token"] for m in ledger["marks"]] == [POPCAT]
    result = analyze_ledger(ledger, WALLET, "solana", now)
    assert result["windows"]["90"]["closed_cycles"] == 15
    assert result["open_tokens"] == 1


def test_an_ordinary_agent_wallet_now_ranks():
    """Airdrops, a self-transfer, a token swap, a batch exit and a 40-day history.

    Every one of these used to disqualify the wallet outright. Together they are
    what an active agent's address actually looks like.
    """
    import time
    from adapters.ledger import build
    from twobots.wallets import rank_wallets
    now = time.time()
    entries = [entry("genesis", int(now - 40 * DAY), pre=[balance(1, USDC, WALLET, 0)],
                     post=[balance(1, USDC, WALLET, 200_000_000_000)])]
    for i in range(12):
        ts = int(now - 38 * DAY + i * 2 * DAY)
        token = [BONK, WIF, POPCAT][i % 3]
        entries.append(usdc_swap(f"b{i}", ts, token, 1000, -500, slot=100 + i))
        entries.append(usdc_swap(f"s{i}", ts + 3600, token, -1000, 500 + (400 if i % 3 == 0 else -100),
                                 slot=200 + i))
    spare = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
    # Unsolicited airdrop, then dumped: profit, but not trading skill.
    entries.append(entry("drop", int(now - 12 * DAY), fee=0, pre=[balance(5, spare, WALLET, 0, 9)],
                         post=[balance(5, spare, WALLET, 10 ** 12, 9)]))
    entries.append(usdc_swap("dump", int(now - 11 * DAY), spare, -1000, 300, slot=500))
    # Buy BONK, rotate it into WIF, then exit both together at one price.
    entries.append(usdc_swap("rot_in", int(now - 9 * DAY), BONK, 800, -400, slot=600))
    entries.append(entry("rotate", int(now - 8 * DAY), fee=0,
                         pre=[balance(2, BONK, WALLET, 800 * 10 ** 9, 9),
                              balance(3, WIF, WALLET, 0, 9)],
                         post=[balance(2, BONK, WALLET, 0, 9),
                               balance(3, WIF, WALLET, 6 * 10 ** 9, 9)]))
    entries.append(usdc_swap("keep", int(now - 7 * DAY), POPCAT, 200, -300, slot=700))
    entries.append(entry("consolidate", int(now - 6 * DAY), fee=0,
                         pre=[balance(1, USDC, WALLET, 0), balance(3, WIF, WALLET, 6 * 10 ** 9, 9),
                              balance(4, POPCAT, WALLET, 200 * 10 ** 9, 9)],
                         post=[balance(1, USDC, WALLET, 900_000_000),
                               balance(3, WIF, WALLET, 0, 9),
                               balance(4, POPCAT, WALLET, 0, 9)]))
    # Moving a position to another of my own wallets: small share of deployed cost.
    entries.append(usdc_swap("cold_buy", int(now - 5 * DAY), BONK, 100, -200, slot=800))
    entries.append(entry("cold", int(now - 4 * DAY), fee=0,
                         pre=[balance(2, BONK, WALLET, 100 * 10 ** 9, 9)],
                         post=[balance(2, BONK, WALLET, 0, 9)]))

    ledger, report = build(WALLET, FakeHelius(entries), FakeSolPrice(), now=now, quote_marks=False)
    assert report["usable"] is True, report
    sides = report["rows_by_side"]
    assert sides["transfer_in"] == 1 and sides["transfer_out"] == 1
    assert sides["swap"] == 1 and sides["batch_sell"] == 1

    result = rank_wallets([analyze_ledger(ledger, WALLET, "solana", now)])[0]
    assert result["status"] == "ranked" and result["score"] is not None
    # The airdrop's 300 less its own gas is reported, but kept out of the trading numbers.
    assert result["windows"]["90"]["external_origin_pnl_usd"] == float(
        300 - Decimal(5000) / 10 ** 9 * SOL_USD)
    assert 0 < result["censored_cost_fraction"] < .25
    assert "record_materially_censored" not in result["flags"]
    assert "allocation_estimated" in result["flags"]


def test_build_reports_an_unusable_wallet_instead_of_raising():
    import time
    from adapters.ledger import build
    now = time.time()
    entries = clean_history(now)
    entries.append(entry("multibuy", int(now - 30 * DAY), fee=0,
                         pre=[balance(1, USDC, WALLET, 900_000_000),
                              balance(5, POPCAT, WALLET, 0, 9), balance(6, WIF, WALLET, 0, 9)],
                         post=[balance(1, USDC, WALLET, 0),
                               balance(5, POPCAT, WALLET, 10 ** 12, 9),
                               balance(6, WIF, WALLET, 10 ** 12, 9)]))
    ledger, report = build(WALLET, FakeHelius(entries), FakeSolPrice(), now=now, quote_marks=False)
    assert ledger is None
    assert report["usable"] is False
    assert report["blocked_by"] == "unreconstructable_cost_basis"
    assert report["reasons"] == {"unallocatable_multi_token_transaction": 1}


def test_build_produces_a_ledger_the_ranking_accepts():
    import time
    from adapters.ledger import build
    now = time.time()
    ledger, report = build(WALLET, FakeHelius(clean_history(now)), FakeSolPrice(),
                           now=now, quote_marks=False)
    assert report["usable"] is True and report["unreconstructable"] == 0
    result = analyze_ledger(ledger, WALLET, "solana", now)
    assert result["windows"]["90"]["closed_cycles"] == 15


def test_build_reports_short_history():
    import time
    from adapters.ledger import build
    now = time.time()
    entries = [usdc_swap("b0", int(now - 10 * DAY), BONK, 1000, -500)]
    ledger, report = build(WALLET, FakeHelius(entries), FakeSolPrice(), now=now, quote_marks=False)
    assert ledger is None
    assert report["blocked_by"] == "history_shorter_than_30_days"
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
