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
                          ledgers_per_cycle=2, ledger_build_refresh_s=86400)
    store = Store(tmp_path)
    try:
        yield cfg, store, tmp_path / "ledgers"
    finally:
        store.close()


def stub_builder(results, monkeypatch, calls=None):
    """Replace the adapter entry points the CLI imports lazily."""
    import adapters.helius, adapters.ledger, adapters.prices

    def build(address, helius, prices, **kwargs):
        if calls is not None:
            calls.append(address)
        return results[address]

    monkeypatch.setattr(adapters.ledger, "build", build)
    monkeypatch.setattr(adapters.helius, "Helius", lambda *a, **k: object())
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


def test_wide_leader_set_without_matching_slots_is_refused(tmp_path):
    """Following 100 wallets through 3 slots drops almost every signal."""
    raw = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    raw = raw.replace("  max_leaders: 3", "  max_leaders: 100")
    path = tmp_path / "wide.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="max_positions"):
        load_config(path)
