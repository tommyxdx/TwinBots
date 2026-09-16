"""`twobots run` wiring: candidates, ledger rebuilds and the report they feed.

Offline. The chain client is a stub; nothing here reaches an RPC endpoint.
"""
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twobots.cli import candidates, refresh_ledgers
from twobots.config import load_config
from twobots.report import build_report
from twobots.storage import Store

ROOT = Path(__file__).resolve().parents[1]
A = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
C = "AQd9LrizjTQrQQuEFRTLqbuZn2ntvAEgA8g9HMALn9Mp"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(ROOT / "config.example.yaml")
    cfg["data_dir"] = str(tmp_path)
    cfg["wallets"].update(ledger_dir=str(tmp_path / "ledgers"), addresses=[],
                          ledgers_per_cycle=2, ledger_build_refresh_s=86400,
                          ledger_calls_per_cycle=1000)
    store = Store(tmp_path)
    try:
        yield cfg, store, tmp_path / "ledgers"
    finally:
        store.close()


def stub_builder(results, monkeypatch, calls=None):
    """Replace the adapter entry points the CLI imports lazily."""
    import adapters.helius, adapters.ledger, adapters.prices

    class Counting:
        def __init__(self):
            self.calls = 0

    def build(address, helius, prices, **kwargs):
        if calls is not None:
            calls.append(address)
        helius.calls += 2          # every screen costs something
        return results[address]

    monkeypatch.setattr(adapters.ledger, "build", build)
    monkeypatch.setattr(adapters.helius, "Helius", lambda *a, **k: Counting())
    monkeypatch.setattr(adapters.prices, "SolPrice", lambda *a, **k: object())


def ledger_for(address):
    return {"schema_version": 1, "address": address, "transactions": [], "marks": []}


def test_candidates_put_configured_addresses_before_discovery(env):
    cfg, store, _ = env
    cfg["wallets"]["addresses"] = [C]
    store.set("wallet:discovery:solana", {"at": time.time(), "addresses": {A: 100, B: 200}})
    # Discovery is ordered newest first; the operator's own list still leads.
    assert candidates(cfg, store) == [C, B, A]


def test_refresh_writes_usable_ledgers_and_records_why_others_failed(env, monkeypatch):
    cfg, store, folder = env
    store.set("wallet:discovery:solana", {"at": time.time(), "addresses": {A: 200, B: 100}})
    stub_builder({A: (ledger_for(A), {"address": A, "usable": True, "rpc_calls": 4}),
                  B: (None, {"address": B, "usable": False, "rpc_calls": 2,
                             "blocked_by": "history_shorter_than_30_days"})}, monkeypatch)
    result = refresh_ledgers(cfg, store)
    assert result["built"] == 1
    assert (folder / (A + ".json")).exists()
    assert not (folder / (B + ".json")).exists()
    state = store.get("wallet:build")
    assert state[A]["usable"] is True
    assert state[B]["blocked_by"] == "history_shorter_than_30_days"


def test_a_wallet_that_stops_qualifying_loses_its_stale_ledger(env, monkeypatch):
    cfg, store, folder = env
    store.set("wallet:discovery:solana", {"at": time.time(), "addresses": {A: 100}})
    stub_builder({A: (ledger_for(A), {"address": A, "usable": True})}, monkeypatch)
    refresh_ledgers(cfg, store)
    assert (folder / (A + ".json")).exists()

    store.set("wallet:build", {})  # force a rebuild
    stub_builder({A: (None, {"address": A, "usable": False,
                             "blocked_by": "unreconstructable_cost_basis"})}, monkeypatch)
    refresh_ledgers(cfg, store)
    assert not (folder / (A + ".json")).exists(), "stale ledger must not keep ranking"


def test_rebuilds_are_bounded_and_rotate_oldest_first(env, monkeypatch):
    cfg, store, _ = env
    store.set("wallet:discovery:solana", {"at": time.time(), "addresses": {A: 300, B: 200, C: 100}})
    results = {a: (ledger_for(a), {"address": a, "usable": True}) for a in (A, B, C)}
    seen = []
    stub_builder(results, monkeypatch, seen)
    refresh_ledgers(cfg, store)
    assert len(seen) == 2, "ledgers_per_cycle caps the spend per wake"
    refresh_ledgers(cfg, store)
    assert sorted(seen) == sorted([A, B, C]), "the untouched candidate comes next"


def test_a_cheap_rejection_does_not_consume_a_whole_slot(env, monkeypatch):
    """A distributor is refused in one call; a backfill costs dozens. Counting
    wallets rather than calls made the cheap screens as expensive as the real
    work and left a hundred candidates queued for hours."""
    import adapters.helius, adapters.ledger, adapters.prices
    cfg, store, _ = env
    everyone = [A, B, C]
    store.set("wallet:discovery:solana",
              {"at": time.time(), "addresses": {a: 100 + i for i, a in enumerate(everyone)}})
    cfg["wallets"].update(ledgers_per_cycle=25, ledger_calls_per_cycle=5)

    class Counting:
        def __init__(self):
            self.calls = 0

    seen = []

    def build(address, helius, prices, **kwargs):
        seen.append(address)
        helius.calls += 1          # every candidate here is a one-call rejection
        return None, {"address": address, "usable": False,
                      "blocked_by": "buys_and_forwards_rather_than_trades"}

    monkeypatch.setattr(adapters.ledger, "build", build)
    monkeypatch.setattr(adapters.helius, "Helius", lambda *a, **k: Counting())
    monkeypatch.setattr(adapters.prices, "SolPrice", lambda *a, **k: object())

    result = refresh_ledgers(cfg, store)
    assert len(seen) == 3, "all three fit inside the call budget"
    assert result["screened"] == 3 and result["built"] == 0
    assert result["blocked"] == {"buys_and_forwards_rather_than_trades": 3}


def test_an_expensive_backfill_stops_the_cycle_at_its_budget(env, monkeypatch):
    import adapters.helius, adapters.ledger, adapters.prices
    cfg, store, _ = env
    store.set("wallet:discovery:solana",
              {"at": time.time(), "addresses": {a: 100 + i for i, a in enumerate([A, B, C])}})
    cfg["wallets"].update(ledgers_per_cycle=25, ledger_calls_per_cycle=5)

    class Counting:
        def __init__(self):
            self.calls = 0

    seen = []

    def build(address, helius, prices, **kwargs):
        seen.append(address)
        helius.calls += 40         # a full history walk
        return ledger_for(address), {"address": address, "usable": True}

    monkeypatch.setattr(adapters.ledger, "build", build)
    monkeypatch.setattr(adapters.helius, "Helius", lambda *a, **k: Counting())
    monkeypatch.setattr(adapters.prices, "SolPrice", lambda *a, **k: object())

    result = refresh_ledgers(cfg, store)
    assert len(seen) == 1, "one backfill exhausts the cycle"
    assert result["pending"] == 2


def test_a_ledger_is_rebuilt_before_the_ranking_calls_it_stale(tmp_path):
    """A ledger records when it was built and the ranking refuses one older than
    max_age_s. Rebuilding less often left every ledger dead for the difference —
    measured at 18 hours out of 24, which reads as nothing ever being ranked."""
    raw = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    raw = raw.replace("  ledger_build_refresh_s: 14400", "  ledger_build_refresh_s: 86400")
    path = tmp_path / "rots.yaml"
    path.write_text(raw, encoding="utf-8")
    w = load_config(path)["wallets"]
    assert w["ledger_build_refresh_s"] < w["max_age_s"], "corrected rather than left to rot"


def test_each_outcome_waits_its_own_interval(env, monkeypatch):
    """A distributor will not stop being one today; rechecking it hourly only
    crowds out the usable ledgers that have to stay fresh."""
    import adapters.helius, adapters.ledger, adapters.prices
    cfg, store, _ = env
    cfg["wallets"].update(ledger_build_refresh_s=100, ledger_retry_s=10,
                          ledger_reject_retry_s=100000, ledger_calls_per_cycle=1000)
    store.set("wallet:discovery:solana",
              {"at": time.time(), "addresses": {A: 300, B: 200, C: 100}})
    now = time.time()
    store.set("wallet:build", {
        A: {"at": now - 50, "usable": True},                          # fresh enough
        B: {"at": now - 50, "usable": False, "error": "RuntimeError: x"},  # retry soon
        C: {"at": now - 50, "usable": False,
            "blocked_by": "buys_and_forwards_rather_than_trades"},    # settled
    })

    class Counting:
        def __init__(self):
            self.calls = 0

    seen = []
    monkeypatch.setattr(adapters.ledger, "build",
                        lambda address, h, p, **k: (seen.append(address),
                                                    (None, {"address": address, "usable": False,
                                                            "blocked_by": "x"}))[1])
    monkeypatch.setattr(adapters.helius, "Helius", lambda *a, **k: Counting())
    monkeypatch.setattr(adapters.prices, "SolPrice", lambda *a, **k: object())
    refresh_ledgers(cfg, store)
    assert seen == [B], "only the transient error is due at 50 seconds"


def test_an_empty_queue_reports_a_cycle_rather_than_raising(env, monkeypatch):
    """The early return skipped the keys the loop logs, so an idle cycle surfaced
    as a warning about a missing key."""
    cfg, store, _ = env
    result = refresh_ledgers(cfg, store)
    assert result == {"screened": 0, "built": 0, "blocked": {}, "rpc_calls": 0, "pending": 0}


def test_a_fresh_ledger_is_not_rebuilt_until_it_goes_stale(env, monkeypatch):
    cfg, store, _ = env
    store.set("wallet:discovery:solana", {"at": time.time(), "addresses": {A: 100}})
    seen = []
    stub_builder({A: (ledger_for(A), {"address": A, "usable": True})}, monkeypatch, seen)
    refresh_ledgers(cfg, store)
    refresh_ledgers(cfg, store)
    assert seen == [A]


def test_report_shows_the_rejection_mix_without_a_separate_pass(env, monkeypatch):
    cfg, store, _ = env
    store.set("wallet:discovery:solana", {"at": time.time(), "addresses": {A: 300, B: 200}})
    stub_builder({A: (ledger_for(A), {"address": A, "usable": True, "rpc_calls": 9}),
                  B: (None, {"address": B, "usable": False, "rpc_calls": 3,
                             "blocked_by": "unreconstructable_cost_basis"})}, monkeypatch)
    refresh_ledgers(cfg, store)
    builds = build_report(cfg, store)["ledger_builds"]
    assert builds["checked"] == 2 and builds["usable"] == 1
    assert builds["rejection_rate"] == 0.5
    assert builds["blocked_by"] == {"unreconstructable_cost_basis": 1}


class FakeEngine:
    """Stands in for a venue: a long-running task plus an exit path."""

    def __init__(self, venue="copy", positions=2, hang=False):
        self.venue, self.positions, self.hang = venue, positions, hang
        self.liquidated, self.cancelled = None, False

    async def run(self):
        import asyncio
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            if self.hang:
                # A socket that will not close in time must not block the exit.
                await asyncio.sleep(3600)
            raise

    async def liquidate(self, reason="shutdown"):
        self.liquidated = reason
        return [{"token": f"t{i}", "status": "FILLED"} for i in range(self.positions)]


def run_supervisor(env, engines, close_positions, stop_after=0.05):
    import asyncio
    from twobots.cli import supervise
    cfg, store, _ = env

    async def main():
        tasks = [asyncio.create_task(e.run()) for e in engines]
        tasks.append(asyncio.create_task(asyncio.sleep(stop_after)))
        await supervise(tasks, engines, cfg, store, close_positions, grace_s=1)

    asyncio.run(main())


def test_shutdown_cancels_every_task_and_writes_a_final_report(env):
    cfg, store, _ = env
    engines = [FakeEngine("cex"), FakeEngine("copy")]
    run_supervisor(env, engines, close_positions=False)
    assert all(e.cancelled for e in engines)
    assert all(e.liquidated is None for e in engines), "positions are kept by default"
    assert (Path(cfg["data_dir"]) / "reports" / "latest.html").exists()


def test_close_positions_sells_before_the_feeds_are_cancelled(env):
    """An exit needs a live book or a live quote, so it has to happen first."""
    cfg, store, _ = env
    engines = [FakeEngine("cex"), FakeEngine("copy")]
    run_supervisor(env, engines, close_positions=True)
    assert all(e.liquidated == "shutdown" for e in engines)
    assert all(e.cancelled for e in engines)


def test_a_task_that_refuses_to_die_cannot_block_the_exit(env):
    import time
    engines = [FakeEngine("copy", hang=True)]
    started = time.time()
    run_supervisor(env, engines, close_positions=False)
    # grace_s is 1, so the whole wind-down stays inside a couple of seconds.
    assert time.time() - started < 5


def test_a_real_sigint_winds_down_inside_the_loop(env):
    """The actual interrupt path: SIGINT must become an event, not unwind the loop.

    Letting KeyboardInterrupt escape leaves the venue feeds' TLS sockets open
    while the loop closes underneath them, which is what produced the SSL
    transport traceback on exit.
    """
    import asyncio
    import signal as signal_module
    from twobots.cli import supervise
    cfg, store, _ = env
    engines = [FakeEngine("cex"), FakeEngine("copy")]
    escaped = []

    async def main():
        tasks = [asyncio.create_task(e.run()) for e in engines]
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, signal_module.raise_signal, signal_module.SIGINT)
        try:
            await supervise(tasks, engines, cfg, store, False, grace_s=1)
        except KeyboardInterrupt:
            escaped.append(True)

    asyncio.run(main())
    assert not escaped, "KeyboardInterrupt must be handled inside the loop"
    assert all(e.cancelled for e in engines)
    assert (Path(cfg["data_dir"]) / "reports" / "latest.html").exists()
    # And the handler is put back, so a second interrupt is not swallowed.
    assert signal_module.getsignal(signal_module.SIGINT) is signal_module.default_int_handler


def test_wide_leader_set_without_matching_slots_is_refused(tmp_path):
    """Following 100 wallets through 3 slots drops almost every signal."""
    raw = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    raw = raw.replace("  max_leaders: 3", "  max_leaders: 100")
    path = tmp_path / "wide.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="max_positions"):
        load_config(path)


def snapshot_rows(store, ts, rows):
    with store.transaction() as db:
        db.executemany("INSERT OR REPLACE INTO rankings VALUES(?,?,?,?,?,?,?,?,?,?)",
                       [(ts, a, rank, score, 10, roi7, None, None, 0.0, "[]")
                        for a, rank, score, roi7 in rows])


def test_forward_test_separates_the_ranking_from_its_own_history(env):
    """The 7-day window read a week later covers the period after the ranking,
    so it is a forward result rather than a restatement of the same history."""
    import time
    from twobots.cli import forward_test
    cfg, store, _ = env
    now = time.time()
    wallets = [A, B, C, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
               "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
               "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"]
    # Ranked best to worst a week ago; the top half then did better.
    snapshot_rows(store, now - 8 * 86400,
                  [(a, i + 1, 50 - i * 5, None) for i, a in enumerate(wallets)])
    snapshot_rows(store, now,
                  [(a, i + 1, 50 - i * 5, 0.30 if i < 3 else -0.10)
                   for i, a in enumerate(wallets)])
    result = forward_test(store)
    assert result["pairs"] == 1
    pair = result["results"][0]
    assert pair["wallets"] == 6
    assert pair["top_half_mean_roi7"] == 0.30
    assert pair["bottom_half_mean_roi7"] == -0.10
    assert pair["separation"] == 0.40


def test_forward_test_says_so_when_there_is_nothing_to_compare(env):
    from twobots.cli import forward_test
    cfg, store, _ = env
    assert forward_test(store)["pairs"] == 0
    assert "two snapshots" in forward_test(store)["note"]


def test_a_ranking_is_snapshotted_once_per_interval(env):
    import time
    from twobots.cli import snapshot_ranking
    cfg, store, _ = env
    cfg["wallets"]["ranking_snapshot_every_s"] = 86400
    store.set("wallet:latest", {"generated_at": time.time(), "ranking": [{
        "address": A, "rank": 1, "score": 12.5, "censored_cost_fraction": 0.0, "flags": [],
        "windows": {"7": {"cost_roi": 0.1}, "30": {"cost_roi": 0.2},
                    "90": {"cost_roi": 0.3, "closed_cycles": 11}}}]})
    assert snapshot_ranking(cfg, store)["snapshot"] is True
    # A ranking that was overwritten cannot be compared against later, but one
    # snapshot per scan would be noise rather than history.
    assert snapshot_ranking(cfg, store)["snapshot"] is False
    assert store.rows("SELECT count(*) AS n FROM rankings")[0]["n"] == 1


def test_the_forward_result_reaches_the_report(env):
    """`run` is what stays up and the report is what gets read, so a result that
    needs its own command to see is a result nobody looks at."""
    import time
    from twobots.report import build_report, export_report
    cfg, store, _ = env
    now = time.time()
    wallets = [A, B, C, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
               "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
               "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"]
    snapshot_rows(store, now - 8 * 86400,
                  [(a, i + 1, 50 - i * 5, None) for i, a in enumerate(wallets)])
    snapshot_rows(store, now,
                  [(a, i + 1, 50 - i * 5, 0.2 if i < 3 else -0.1)
                   for i, a in enumerate(wallets)])
    assert build_report(cfg, store)["forward_test"]["results"][0]["separation"] == 0.3
    html = Path(export_report(cfg, store)).read_text(encoding="utf-8")
    assert "排名前瞻检验" in html


def test_the_report_says_so_before_there_is_anything_to_compare(env):
    from twobots.report import export_report
    cfg, store, _ = env
    html = Path(export_report(cfg, store)).read_text(encoding="utf-8")
    assert "两" in html or "two snapshots" in html or "快照" in html
