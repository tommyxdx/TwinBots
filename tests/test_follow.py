"""Copy trading: leader eligibility, lag gating, mirrored exits and replay safety.

Offline. The quote gateway is a stub; no network, no signing, no real orders.
"""
import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twobots.config import load_config
from twobots.follow import ActivityFeed, CopyTrader
from twobots.storage import Store
from twobots.wallet_scanner import WalletScanner
from twobots.wallets import BASE58, DAY

NOW = time.time()
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PRICE_USD = 0.001
TOKEN_DECIMALS = 10 ** 6


def address(number):
    raw = number.to_bytes(32, "big")
    encoded, n = "", number
    while n:
        n, digit = divmod(n, 58)
        encoded = BASE58[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def ledger(number, cycles, marks=None):
    """cycles: list of (token_index, realised_pnl_usd) clean buy -> full sell round trips."""
    txs = []
    for i, (token, pnl) in enumerate(cycles):
        ts = NOW - 85 * DAY + i * 2 * DAY
        txs.append({"id": f"{i}:b", "ts": ts, "token": address(token), "side": "buy",
                    "quantity": "1", "notional_usd": "500", "fee_usd": "0"})
        txs.append({"id": f"{i}:s", "ts": ts + 3600, "token": address(token), "side": "sell",
                    "quantity": "1", "notional_usd": str(500 + pnl), "fee_usd": "0"})
    for j, (token, ts, cost) in enumerate(marks or []):
        txs.append({"id": f"open:{j}", "ts": ts, "token": address(token), "side": "buy",
                    "quantity": "1", "notional_usd": str(cost), "fee_usd": "0"})
    return {"schema_version": 1, "currency": "USD", "network": "solana", "address": address(number),
            "source": "SYNTHETIC_TEST_ONLY", "asof": NOW, "history_start": NOW - 120 * DAY,
            "quality": {k: True for k in ("complete", "initial_inventory_empty", "all_protocols",
                                          "fees_included", "transfers_included",
                                          "failed_transactions_included")},
            "transactions": sorted(txs, key=lambda r: r["ts"]),
            "marks": [{"token": address(t), "quantity": "1", "value_usd": "0", "asof": NOW - 60}
                      for t, _, _ in (marks or [])]}


class StubGateway:
    """Constant-price quotes. Records every call so lag behaviour stays observable."""

    def __init__(self):
        self.calls = []

    def quote(self, input_token, output_token, amount):
        self.calls.append((input_token, output_token, amount))
        if input_token == USDC:
            out = int(amount / TOKEN_DECIMALS * TOKEN_PRICE_USD ** -1 * TOKEN_DECIMALS)
        else:
            out = int(amount / TOKEN_DECIMALS * TOKEN_PRICE_USD * TOKEN_DECIMALS)
        return {"input_token": input_token, "output_token": output_token, "in_amount": amount,
                "out_amount": out, "min_out": int(out * 0.99), "price_impact_fraction": 0.001,
                "asof": time.time(), "http_latency_s": 0.0}


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("Copy trading tests must never access a network")


def write_activity(folder, leader, fills, asof=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (leader + ".json")).write_text(json.dumps(
        {"schema_version": 1, "network": "solana", "address": leader,
         "asof": asof if asof is not None else time.time(),
         "source": "SYNTHETIC_TEST_ONLY", "fills": fills}), encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    cfg["data_dir"] = str(tmp_path)
    ledgers, activity = tmp_path / "ledgers", tmp_path / "activity"
    ledgers.mkdir()
    cfg["wallets"].update(ledger_dir=str(ledgers), addresses=[], discover_enabled=False,
                          max_wallets_per_run=50)
    cfg["follow"].update(enabled=True, source="local", activity_dir=str(activity),
                         max_leaders=3, min_score=10, allowed_flags=[], cooldown_s=3600,
                         max_signal_age_s=120, max_feed_age_s=300, min_leader_notional_usd=50,
                         ticket_usd=2, max_positions=3, initial_cash=100)
    cfg["dex"].update(dropped_probability=0, revert_probability=0, latency_ms=[0, 0])
    store = Store(tmp_path)
    try:
        yield cfg, store, ledgers, activity
    finally:
        store.close()


def rank(cfg, store, ledgers, wallets):
    for data in wallets:
        (ledgers / (data["address"] + ".json")).write_text(json.dumps(data), encoding="utf-8")
    return WalletScanner(cfg, store, NoNetwork(), NoNetwork()).run_once()


def trader(cfg, store, gateway=None):
    return CopyTrader(cfg, store, gateway or StubGateway(), ActivityFeed(cfg, NoNetwork()))


# 30 cycles over 10 tokens, 30% win rate, 4:1 payoff. Flat at the end, no flags.
GOOD = [(100 + i % 10, 400 if i % 10 < 3 else -100) for i in range(30)]
# Same cadence but realises only winners while sitting on 30 worthless bags.
BAGS = [(100 + i % 10, 400) for i in range(30)]
BAG_MARKS = [(300 + j, NOW - 40 * DAY + j * 3600, 500) for j in range(30)]


def test_bag_holder_is_not_ranked_and_never_followed(env):
    """Realising winners while holding losers must not buy a leader slot."""
    cfg, store, ledgers, activity = env
    result = rank(cfg, store, ledgers, [ledger(700, GOOD), ledger(701, BAGS, BAG_MARKS)])
    scored = {r["address"]: r for r in result["ranking"]}
    good, bag = scored[address(700)], scored[address(701)]
    assert good["status"] == "ranked" and good["rank"] == 1
    assert bag["status"] == "observation" and bag["score"] is None
    assert "realized_profit_does_not_cover_open_losses" in bag["flags"]
    assert bag["windows"]["90"]["realized_pnl_usd"] + bag["open_loss_usd"] < 0
    assert trader(cfg, store).leaders(time.time()) == [address(700)]


def test_fresh_leader_buy_opens_a_paper_position(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    leader, token = address(700), address(900)
    write_activity(activity, leader, [{"id": "sig1", "ts": time.time() - 5, "side": "buy",
                                       "token": token, "notional_usd": "250"}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    state = bot.ledger.state()
    assert token in state["positions"], state
    position = state["positions"][token]
    assert position["leader"] == leader
    assert position["copy_lag_s"] < cfg["follow"]["max_signal_age_s"]
    assert state["cash"] < cfg["follow"]["initial_cash"]


def test_stale_signal_is_skipped_and_never_fires_later(env):
    """A fill older than max_signal_age_s is the leader's move, not ours."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    leader, token = address(700), address(900)
    write_activity(activity, leader, [{"id": "old", "ts": time.time() - 600, "side": "buy",
                                       "token": token, "notional_usd": "250"}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert bot.ledger.state()["positions"] == {}
    # Re-publishing the same fill with a fresh feed timestamp must stay inert.
    write_activity(activity, leader, [{"id": "old", "ts": time.time() - 600, "side": "buy",
                                       "token": token, "notional_usd": "250"}])
    asyncio.run(bot.step())
    assert bot.ledger.state()["positions"] == {}


def test_repeated_fill_id_enters_once(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    leader, token = address(700), address(900)
    fill = [{"id": "sig1", "ts": time.time() - 5, "side": "buy", "token": token, "notional_usd": "250"}]
    write_activity(activity, leader, fill)
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    cash = bot.ledger.state()["cash"]
    write_activity(activity, leader, fill)
    asyncio.run(bot.step())
    assert bot.ledger.state()["cash"] == cash
    entries = store.rows("SELECT payload FROM events WHERE kind='copy_entry_result'")
    assert len(entries) == 1


def test_leader_sell_mirrors_the_exit(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    leader, token = address(700), address(900)
    write_activity(activity, leader, [{"id": "in", "ts": time.time() - 5, "side": "buy",
                                       "token": token, "notional_usd": "250"}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert token in bot.ledger.state()["positions"]
    write_activity(activity, leader, [{"id": "out", "ts": time.time() - 2, "side": "sell",
                                       "token": token, "notional_usd": "250"}])
    bot.next_mark = time.time() + 3600  # isolate the mirrored exit from the risk marker
    asyncio.run(bot.step())
    assert token not in bot.ledger.state()["positions"]


def test_small_leader_ticket_is_ignored(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    write_activity(activity, address(700), [{"id": "dust", "ts": time.time() - 5, "side": "buy",
                                             "token": address(900), "notional_usd": "5"}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert bot.ledger.state()["positions"] == {}


def test_stale_ranking_suspends_copying(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    report = store.get("wallet:latest")
    report["generated_at"] = time.time() - cfg["follow"]["ranking_max_age_s"] - 1
    store.set("wallet:latest", report)
    assert trader(cfg, store).leaders(time.time()) == []


@pytest.mark.parametrize("mutation", [
    {"schema_version": 2},
    {"network": "ethereum"},
    {"address": address(999)},
    {"fills": [{"id": "x", "ts": NOW, "side": "transfer", "token": address(900), "notional_usd": "1"}]},
    {"fills": [{"id": "x", "ts": NOW, "side": "buy", "token": "not-an-address", "notional_usd": "1"}]},
    {"fills": [{"id": "x", "ts": NOW, "side": "buy", "token": address(900), "notional_usd": "-1"}]},
    {"fills": [{"id": "x", "ts": NOW, "side": "buy", "token": address(900), "notional_usd": "1"},
               {"id": "x", "ts": NOW, "side": "buy", "token": address(901), "notional_usd": "1"}]},
])
def test_malformed_activity_is_rejected(env, mutation):
    cfg, store, ledgers, activity = env
    leader = address(700)
    activity.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "network": "solana", "address": leader, "asof": time.time(),
               "source": "SYNTHETIC_TEST_ONLY",
               "fills": [{"id": "ok", "ts": time.time() - 5, "side": "buy",
                          "token": address(900), "notional_usd": "250"}], **mutation}
    (activity / (leader + ".json")).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        ActivityFeed(cfg, NoNetwork()).fills(leader, time.time())


def test_feed_failure_does_not_halt_the_loop(env):
    """A missing leader file is an absence of signal, not a trading decision."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    activity.mkdir(parents=True, exist_ok=True)
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert bot.ledger.state()["positions"] == {}
    assert store.rows("SELECT 1 FROM events WHERE kind='copy_activity_missing'")


def test_unsellable_token_does_not_drop_remaining_signals(env):
    """A leader exit we cannot quote must not silently discard the other fills."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    leader, held, other = address(700), address(900), address(901)
    write_activity(activity, leader, [{"id": "in", "ts": time.time() - 5, "side": "buy",
                                       "token": held, "notional_usd": "250"}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert held in bot.ledger.state()["positions"]

    class BrokenSell(StubGateway):
        def quote(self, input_token, output_token, amount):
            if input_token == held:
                raise RuntimeError("no route")
            return super().quote(input_token, output_token, amount)

    bot.gateway = BrokenSell()
    bot.next_mark = time.time() + 3600
    write_activity(activity, leader, [{"id": "out", "ts": time.time() - 3, "side": "sell",
                                       "token": held, "notional_usd": "250"},
                                      {"id": "in2", "ts": time.time() - 2, "side": "buy",
                                       "token": other, "notional_usd": "250"}])
    asyncio.run(bot.step())
    state = bot.ledger.state()
    assert held in state["positions"], "unsellable inventory is retained, not deleted"
    assert other in state["positions"], "the later buy signal still had to be processed"
    assert store.rows("SELECT 1 FROM events WHERE kind='copy_exit_unavailable'")


def test_idle_reason_names_the_gate_that_actually_stopped_it(env):
    """Silence is indistinguishable from a hang, so each gate must say so."""
    cfg, store, ledgers, activity = env
    bot = trader(cfg, store)
    now = time.time()
    assert "not produced a ranking" in bot.idle_reason(now)
    # Nothing reconstructed at all reads differently from nothing qualifying.
    rank(cfg, store, ledgers, [])
    assert "no ledger reconstructed yet" in bot.idle_reason(now)

    # A wallet that was analysed and refused is not the same as one never seen,
    # and the message has to say which gate stopped it.
    rank(cfg, store, ledgers, [ledger(701, BAGS, BAG_MARKS)])
    reason = bot.idle_reason(now)
    assert "1 analysed, none qualified" in reason
    assert "realized_profit_does_not_cover_open_losses" in reason
    assert "closed cycles" in reason

    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    cfg["follow"]["min_score"] = 999
    assert "below min_score" in bot.idle_reason(now)

    cfg["follow"]["min_score"] = 10
    report = store.get("wallet:latest")
    report["generated_at"] = now - cfg["follow"]["ranking_max_age_s"] - 1
    store.set("wallet:latest", report)
    assert bot.idle_reason(now) == "ranking is stale"


def test_flagged_only_ranking_reports_the_flag_gate(env):
    cfg, store, ledgers, activity = env
    # A one-token wallet ranks, but carries a flag that allowed_flags does not admit.
    rank(cfg, store, ledgers, [ledger(702, [(100, 10000)] + [(100 + i % 9 + 1, -10)
                                                             for i in range(29)])])
    bot = trader(cfg, store)
    assert bot.leaders(time.time()) == []
    assert "allowed_flags" in bot.idle_reason(time.time())


def test_copy_account_is_separate_from_dex(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    write_activity(activity, address(700), [{"id": "sig1", "ts": time.time() - 5, "side": "buy",
                                             "token": address(900), "notional_usd": "250"}])
    asyncio.run(trader(cfg, store).step())
    assert store.get("account:copy")["cash"] < cfg["follow"]["initial_cash"]
    assert store.get("account:dex") is None
