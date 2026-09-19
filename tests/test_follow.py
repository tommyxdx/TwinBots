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
    # Spread across the whole 85-day history and up to the present, so a wallet
    # the fixtures call followable has recent fills as well as a long record.
    step = 84 * DAY / max(1, len(cycles) - 1)
    for i, (token, pnl) in enumerate(cycles):
        ts = NOW - 85 * DAY + i * step
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


def at_leader_price(notional_usd):
    """The raw quantity a leader receives at the stub's own price: no chase."""
    return str(int(float(notional_usd) * 10 ** 6 / TOKEN_PRICE_USD))


def write_activity(folder, leader, fills, asof=None):
    # Tests about other gates get a leader who paid what we would pay, so the
    # chase gate stays out of their way; chase tests supply their own quantity.
    fills = [{**f, "quantity_raw": at_leader_price(f["notional_usd"])}
             if "quantity_raw" not in f and f.get("side") in ("buy", "sell")
             and float(f.get("notional_usd") or 0) > 0 else f for f in fills]
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
    # Naming the gate that actually fired beats naming the only one there used
    # to be: selection now refuses for several different reasons.
    assert "flagged" in bot.idle_reason(time.time())


def quiet_since(data, days):
    """The same wallet with nothing traded in the last `days` days."""
    shifted = dict(data)
    shifted["transactions"] = [{**t, "ts": t["ts"] - days * DAY} for t in data["transactions"]]
    return shifted


def test_a_wallet_that_has_stopped_trading_is_not_followed(env):
    """A 90-day record says a wallet could trade, not that it still does. The
    top-ranked wallet on the live box had made twelve transactions in a week and
    was mostly receiving transfers: following it produced no signals at all
    while holding a leader slot an active wallet would have used."""
    cfg, store, ledgers, activity = env
    cfg["follow"]["min_recent_fills"] = 4
    rank(cfg, store, ledgers, [quiet_since(ledger(704, GOOD), 30)])
    bot = trader(cfg, store)
    assert bot.leaders(time.time()) == []
    assert "dormant" in bot.idle_reason(time.time())
    # The same wallet still trading is followed, so the gate is about recency.
    rank(cfg, store, ledgers, [ledger(705, GOOD)])
    assert address(705) in trader(cfg, store).leaders(time.time())


def test_retention_refuses_a_wallet_that_keeps_dropping_out(env):
    """One good ranking is what a lucky streak looks like. Surviving several
    independent rankings is the cheapest evidence of the opposite."""
    cfg, store, ledgers, activity = env
    cfg["follow"].update(min_retention=0.5, min_retention_snapshots=4, retention_snapshots=7)
    rank(cfg, store, ledgers, [ledger(706, GOOD)])
    who = address(706)
    bot = trader(cfg, store)
    assert who in bot.leaders(time.time()), "no history yet, so nothing is held against it"

    # Analysed in five earlier rankings, scored in only one of them.
    with store.transaction() as db:
        for i in range(5):
            db.execute("INSERT INTO rankings(ts,address,rank,score) VALUES(?,?,?,?)",
                       (time.time() - (i + 1) * DAY, who, 1 if i == 0 else None, 40.0))
    bot = trader(cfg, store)
    assert bot.leaders(time.time()) == []
    assert "not_retained" in bot.idle_reason(time.time())


def test_a_newly_reconstructed_wallet_is_not_punished_for_having_no_history(env):
    """Retention counts the rankings that looked at the wallet, not every
    ranking. Counting every one would refuse each new wallet forever, which is
    the same starvation bug in a different place."""
    cfg, store, ledgers, activity = env
    cfg["follow"].update(min_retention=0.5, min_retention_snapshots=4)
    rank(cfg, store, ledgers, [ledger(707, GOOD)])
    other = address(999)
    with store.transaction() as db:
        for i in range(6):
            db.execute("INSERT INTO rankings(ts,address,rank,score) VALUES(?,?,?,?)",
                       (time.time() - (i + 1) * DAY, other, 1, 40.0))
    assert address(707) in trader(cfg, store).leaders(time.time())


def test_one_busy_leader_cannot_spend_the_whole_book(env):
    """Raising max_leaders is what lifts the signal rate; the per-leader slice
    is what stops the first burst consuming the cash before the other leaders
    are heard from."""
    cfg, store, ledgers, activity = env
    cfg["follow"].update(max_leaders=4, initial_cash=100, ticket_usd=10, leader_share=0.0)
    bot = trader(cfg, store)
    assert bot.leader_budget(["A", "B", "C", "D"]) == 25, "an even split across four leaders"
    # With one leader there is nobody to protect, so the book is not left idle.
    assert bot.leader_budget(["A"]) == 100
    state = {"positions": {"t1": {"leader": "A", "last_value": 20},
                           "t2": {"leader": "B", "last_value": 40}}}
    assert bot.leader_exposure(state, "A") == 20
    assert bot.leader_exposure(state, "B") == 40
    assert bot.leader_exposure(state, "C") == 0
    # A explicit share overrides the even split.
    cfg["follow"]["leader_share"] = 0.1
    assert trader(cfg, store).leader_budget(["A"]) == 10, "an explicit share pins it"


def test_the_report_keeps_the_platform_cost_even_when_it_is_not_charged(env):
    """Measuring the theoretical edge first is the right order, but the number
    is only useful beside the cost of capturing it. Recording it as the run goes
    means that comparison never needs the run repeated."""
    from twobots.report import platform_fee_note, refused_costs
    cfg, store, ledgers, activity = env
    cfg["follow"].update(platform_fee_fraction=0.0, platform_fee_reference=0.01, ticket_usd=10)
    note = platform_fee_note(cfg, 6)
    assert "$1.20" in note, "6 fills x $10 x two legs x 1%"
    assert "未计入" in note and "0.00%" in note
    cfg["follow"]["platform_fee_fraction"] = 0.01
    assert "已计入" in platform_fee_note(cfg, 6)
    assert platform_fee_note(cfg, 0) == "", "nothing filled, nothing to say"
    # And the refused round-trips are kept, so a screen that refuses everything
    # is distinguishable from a market that is genuinely too dear.
    spread = refused_costs([{"status": "ROUNDTRIP_COST_REJECTED", "cost_fraction": 0.031},
                            {"status": "ROUNDTRIP_COST_REJECTED", "cost_fraction": 0.09},
                            {"status": "FILLED"}])
    assert "被拒 2 次" in spread and "3.10%" in spread and "9.00%" in spread
    assert refused_costs([{"status": "FILLED"}]) == ""


def paid_over_leader(notional_usd, times):
    """A leader quantity such that we pay `times` what the leader did per token."""
    return str(int(float(notional_usd) * 10 ** 6 / TOKEN_PRICE_USD * times))


def shadow(cfg, store):
    from twobots.follow import ShadowTrader
    return ShadowTrader(cfg, store, StubGateway(), ActivityFeed(cfg, NoNetwork()))


def results(store, venue):
    return [json.loads(r["payload"]) for r in store.rows(
        "SELECT payload FROM events WHERE kind=? ORDER BY id", (venue + "_entry_result",))]


def test_chase_is_measured_against_what_the_leader_actually_paid():
    """The two losing copies on the live box paid 4.67x and 3.24x the leader's
    price for the same token; the one winner paid 1.06x. Raw units per dollar,
    so no decimals are needed and tiny prices compare exactly."""
    from twobots.follow import chase_fraction
    signal = lambda qty: {"notional_usd": 100.0, "quantity_raw": qty}
    # We get 1,000 raw per dollar. A leader who got 4,670 per dollar paid 4.67x less.
    assert abs(chase_fraction(signal(467_000), 10.0, 10_000) - 3.67) < 1e-9
    assert abs(chase_fraction(signal(106_400), 10.0, 10_000) - 0.064) < 1e-9
    # The price has since fallen below the leader's: negative, and never refused.
    assert chase_fraction(signal(50_000), 10.0, 10_000) < 0
    assert chase_fraction({"notional_usd": 100.0, "quantity_raw": None}, 10.0, 10_000) is None
    assert chase_fraction({"notional_usd": 0.0, "quantity_raw": 5}, 10.0, 10_000) is None


def test_a_signal_already_priced_past_the_leader_is_not_followed(env):
    """Buying the top of the leader's own impact is how both live losses happened."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(720, GOOD)])
    leader, token = address(720), address(920)
    write_activity(activity, leader, [{"id": "pump", "ts": time.time() - 5, "side": "buy",
                                       "token": token, "notional_usd": "250",
                                       "quantity_raw": paid_over_leader(250, 4.67)}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert token not in bot.ledger.state()["positions"]
    [result] = results(store, "copy")
    assert result["status"] == "CHASE_REJECTED"
    assert abs(result["chase"] - 3.67) < 0.01, "the refusal records how far the price had run"
    assert result["uid"] == leader + ":pump", "keyed by signal, so the two books count once"


def test_a_signal_near_the_leader_price_is_followed(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(721, GOOD)])
    leader, token = address(721), address(921)
    write_activity(activity, leader, [{"id": "clip", "ts": time.time() - 5, "side": "buy",
                                       "token": token, "notional_usd": "250",
                                       "quantity_raw": paid_over_leader(250, 1.06)}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    assert token in bot.ledger.state()["positions"]
    order = json.loads(store.rows("SELECT payload FROM orders WHERE venue='copy' AND side='BUY'")[0]["payload"])
    assert abs(order["chase"] - 0.06) < 0.01 and order["leader"] == leader


def test_an_unknown_leader_price_is_refused_by_the_gated_book_only(env):
    """A feed that cannot say what the leader paid cannot show that the move is
    still ahead of us. The trading book refuses it; the shadow still takes it, so
    what the refusal cost remains measurable."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(722, GOOD)])
    leader, token = address(722), address(922)
    fill = {"id": "blind", "ts": time.time() - 5, "side": "buy", "token": token, "notional_usd": "250"}
    activity.mkdir(parents=True, exist_ok=True)
    (activity / (leader + ".json")).write_text(json.dumps(
        {"schema_version": 1, "network": "solana", "address": leader, "asof": time.time(),
         "source": "SYNTHETIC_TEST_ONLY", "fills": [fill]}), encoding="utf-8")
    gated, measured = trader(cfg, store), shadow(cfg, store)
    asyncio.run(gated.step())
    asyncio.run(measured.step())
    assert results(store, "copy")[0]["status"] == "CHASE_UNVERIFIABLE"
    assert token not in gated.ledger.state()["positions"]
    assert token in measured.ledger.state()["positions"]


def test_the_shadow_takes_what_the_gate_refuses_and_both_see_the_signal(env):
    """Per-book keys: sharing `copy:seen` would let whichever book stepped first
    swallow the signal for the other. And only the shadow can say what a refused
    trade would have made, because it is the only one that takes it."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(723, GOOD)])
    leader, token = address(723), address(923)
    write_activity(activity, leader, [{"id": "pump", "ts": time.time() - 5, "side": "buy",
                                       "token": token, "notional_usd": "250",
                                       "quantity_raw": paid_over_leader(250, 3.24)}])
    gated, measured = trader(cfg, store), shadow(cfg, store)
    asyncio.run(gated.step())
    asyncio.run(measured.step())
    assert token not in gated.ledger.state()["positions"]
    assert token in measured.ledger.state()["positions"]
    assert results(store, "copy")[0]["status"] == "CHASE_REJECTED"
    assert results(store, "shadow")[0]["settled_status"] == "FILLED"
    assert store.get("copy:seen") and store.get("shadow:seen"), "each book keeps its own"


def test_the_shadow_ignores_halts_budgets_and_the_one_entry_limit(env):
    """The live book halted after two losses and measured nothing for twelve
    hours. The shadow's job is to keep measuring through exactly that."""
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(724, GOOD)])
    leader = address(724)
    fills = [{"id": f"s{i}", "ts": time.time() - 5, "side": "buy", "token": address(924 + i),
              "notional_usd": "250"} for i in range(4)]
    write_activity(activity, leader, fills)
    measured = shadow(cfg, store)
    # A drawdown ceiling of 100% can only be reached by losing everything, and
    # no leader's slice ever fills, so neither limit can stop it measuring.
    assert measured.c["max_drawdown"] == 1.0 and measured.leader_budget([leader]) == float("inf")
    asyncio.run(measured.step())
    assert len(measured.ledger.state()["positions"]) == 4, "every signal, not one per step"


def test_a_leader_whose_profit_is_its_own_impact_is_dropped(env):
    """Screening at the signal only stops us buying one pump; screening the
    leader stops us following a wallet whose whole record is pumps."""
    cfg, store, ledgers, activity = env
    cfg["follow"].update(max_chase_fraction=0.25, min_chase_samples=5, chase_window_days=7)
    rank(cfg, store, ledgers, [ledger(725, GOOD)])
    leader = address(725)
    assert leader in trader(cfg, store).leaders(time.time()), "no record yet, so followed"
    for i in range(6):
        for venue in ("copy", "shadow"):   # both books record the same signal
            store.event(venue + "_entry_result",
                        {"leader": leader, "token": address(930 + i), "uid": f"{leader}:{i}",
                         "chase": 2.5, "status": "CHASE_REJECTED"})
    bot = trader(cfg, store)
    assert bot.leaders(time.time()) == []
    assert "impact_trader" in bot.idle_reason(time.time())
    # The shadow keeps following it: otherwise nothing could show what dropping
    # an impact trader was worth, and its record would lapse and readmit it.
    assert leader in shadow(cfg, store).leaders(time.time())
    # Counted once per signal: six, not twelve. Below the sample floor it is kept.
    assert len(bot.impact_record(time.time() + 301)[leader]) == 6
    cfg["follow"]["min_chase_samples"] = 7
    assert leader in trader(cfg, store).leaders(time.time())


def test_a_leader_trading_near_its_own_fills_is_kept(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(726, GOOD)])
    leader = address(726)
    for i in range(8):
        store.event("shadow_entry_result", {"leader": leader, "token": address(940 + i),
                                            "uid": f"{leader}:{i}", "chase": 0.05,
                                            "settled_status": "FILLED"})
    assert leader in trader(cfg, store).leaders(time.time())


def test_the_chase_card_splits_shadow_trades_by_what_the_gate_would_do(env):
    from twobots.report import chase_card
    cfg, store, ledgers, activity = env
    trip = lambda chase, pnl: {"chase": chase, "pnl": pnl, "return": pnl / 8}
    card = chase_card(cfg, [trip(0.05, 3.0)],
                      [trip(0.06, 24.0), trip(0.1, -1.0), trip(3.67, -5.0), trip(2.24, -6.2)])
    assert "闸门放行" in card and "闸门拦下" in card
    assert "$+23.00" in card, "the let-through group, summed"
    assert "$-11.20" in card, "the refused group: both live losses"
    assert chase_card(cfg, [], []) == ""


def test_the_platform_fee_is_charged_on_every_leg(env):
    """Copying through a service costs a share of each entry and exit, and that
    is the cost that decides whether a thin edge survives. Simulating without it
    measures a strategy nobody can actually buy."""
    cfg, store, ledgers, activity = env
    cfg["follow"]["platform_fee_fraction"] = 0.01
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    leader, token = address(700), address(900)
    write_activity(activity, leader, [{"id": "in", "ts": time.time() - 5, "side": "buy",
                                       "token": token, "notional_usd": "250"}])
    bot = trader(cfg, store)
    asyncio.run(bot.step())
    ticket, gas = cfg["follow"]["ticket_usd"], cfg["dex"]["gas_usd_per_tx"]
    spent = cfg["follow"]["initial_cash"] - bot.ledger.state()["cash"]
    # Ticket, chain cost, and one per cent of the ticket.
    assert abs(spent - (ticket + gas + ticket * 0.01)) < 1e-9

    free = trader(cfg, store)
    free.f = {**cfg["follow"], "platform_fee_fraction": 0.0}
    assert free.venue_fee("BUY", free.raw_usd(ticket), {}) < bot.venue_fee(
        "BUY", bot.raw_usd(ticket), {})


def test_copy_account_is_separate_from_dex(env):
    cfg, store, ledgers, activity = env
    rank(cfg, store, ledgers, [ledger(700, GOOD)])
    write_activity(activity, address(700), [{"id": "sig1", "ts": time.time() - 5, "side": "buy",
                                             "token": address(900), "notional_usd": "250"}])
    asyncio.run(trader(cfg, store).step())
    assert store.get("account:copy")["cash"] < cfg["follow"]["initial_cash"]
    assert store.get("account:dex") is None
