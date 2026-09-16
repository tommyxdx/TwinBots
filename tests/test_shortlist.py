"""Candidate shortlist: extraction, merging, and what happens when a provider fails.

Offline. No provider is contacted; the HTTP client is a stub.
"""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.shortlist import collect, extract, headers_for, rank_by_agreement
from twobots.config import load_config

ROOT = Path(__file__).resolve().parents[1]
A = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
C = "AQd9LrizjTQrQQuEFRTLqbuZn2ntvAEgA8g9HMALn9Mp"


class StubHTTP:
    """Returns a canned body per URL, or raises what the real client would."""

    def __init__(self, bodies):
        self.bodies, self.seen = bodies, []

    def request(self, url, params=None, headers=None, body=None, max_bytes=None):
        self.seen.append((url, params, headers))
        value = self.bodies[url]
        if isinstance(value, Exception):
            raise value
        return json.dumps(value).encode()


def test_extract_walks_the_shapes_each_provider_actually_returns():
    dune = {"result": {"rows": [{"trader": A, "pnl": 1}, {"trader": B, "pnl": 2}]}}
    assert extract(dune, "result.rows[].trader") == [A, B]
    rest = {"data": {"items": [{"address": A}, {"address": B}]}}
    assert extract(rest, "data.items[].address") == [A, B]
    flat = {"data": [{"address": A}]}
    assert extract(flat, "data[].address") == [A]
    # A path that does not match is empty, never an exception mid-merge.
    assert extract(rest, "result.rows[].trader") == []


def test_a_source_without_its_key_is_skipped_not_failed(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.setenv("PRESENT_KEY", "secret")
    headers, missing = headers_for({"headers": {"X-Api": "env:MISSING_KEY"}})
    assert missing == ["MISSING_KEY"] and headers == {}
    headers, missing = headers_for({"headers": {"X-Api": "env:PRESENT_KEY", "x-chain": "solana"}})
    assert missing == [] and headers == {"X-Api": "secret", "x-chain": "solana"}


def test_sources_merge_and_record_who_named_each_address(monkeypatch):
    monkeypatch.setenv("K1", "k1")
    monkeypatch.setenv("K2", "k2")
    http = StubHTTP({
        "https://one/": {"result": {"rows": [{"trader": A}, {"trader": B}]}},
        "https://two/": {"data": {"items": [{"address": B}, {"address": C}]}},
    })
    sources = [
        {"name": "dune", "kind": "http", "url": "https://one/",
         "headers": {"X-DUNE-API-KEY": "env:K1"}, "address_path": "result.rows[].trader"},
        {"name": "birdeye", "kind": "http", "url": "https://two/",
         "headers": {"X-API-KEY": "env:K2"}, "address_path": "data.items[].address"},
    ]
    merged, report = collect(sources, http, now=100)
    assert set(merged) == {A, B, C}
    assert merged[B]["sources"] == ["dune", "birdeye"], "provenance is kept per address"
    assert all(r["valid"] for r in report)
    # Agreement between independent providers goes first.
    assert rank_by_agreement(merged, 10)[0] == B


def test_one_provider_failing_does_not_lose_the_others(monkeypatch):
    monkeypatch.setenv("K1", "k1")
    monkeypatch.delenv("K2", raising=False)
    http = StubHTTP({
        "https://ok/": {"result": {"rows": [{"trader": A}]}},
        "https://broken/": RuntimeError("HTTP 500 from provider"),
    })
    sources = [
        {"name": "good", "kind": "http", "url": "https://ok/",
         "headers": {"X": "env:K1"}, "address_path": "result.rows[].trader"},
        {"name": "down", "kind": "http", "url": "https://broken/",
         "address_path": "result.rows[].trader"},
        {"name": "keyless", "kind": "http", "url": "https://ok/",
         "headers": {"X": "env:K2"}, "address_path": "result.rows[].trader"},
    ]
    merged, report = collect(sources, http, now=100)
    assert set(merged) == {A}
    by_name = {r["source"]: r for r in report}
    assert by_name["down"]["error"] == "RuntimeError"
    assert "K2" in by_name["keyless"]["skipped"]


def test_provider_errors_never_echo_the_request():
    """A provider message can contain the query string, and that carries the key."""
    http = StubHTTP({"https://leaky/": RuntimeError("failed for ?api_key=SECRET123")})
    _, report = collect([{"name": "leaky", "kind": "http", "url": "https://leaky/",
                          "address_path": "a[].b"}], http, now=100)
    assert "SECRET123" not in json.dumps(report)
    assert report[0]["error"] == "RuntimeError"


def test_our_own_status_codes_survive_because_they_carry_no_secret():
    """401 means the key is wrong, 403 means blocked, 404 means the path is.

    Telling those apart is the whole of diagnosing a source, and the client
    builds these messages itself from the code and hostname only.
    """
    http = StubHTTP({
        "https://a/": RuntimeError("HTTP 401 from api.dune.com; inspect endpoint/access locally"),
        "https://b/": RuntimeError("HTTP 403 from api.dune.com; inspect endpoint/access locally"),
        "https://c/": RuntimeError("Network timeout/error from api.dune.com"),
    })
    _, report = collect([{"name": n, "kind": "http", "url": u, "address_path": "a[].b"}
                         for n, u in (("key", "https://a/"), ("blocked", "https://b/"),
                                      ("slow", "https://c/"))], http, now=100)
    errors = {r["source"]: r["error"] for r in report}
    assert "401" in errors["key"] and "403" in errors["blocked"]
    assert errors["slow"].startswith("Network timeout")


def test_probe_finds_the_address_path_without_reading_any_docs():
    """Every provider nests addresses differently; recognising them beats a schema."""
    from adapters.shortlist import probe
    birdeye_shaped = {"success": True, "data": {"items": [
        {"address": A, "pnl": 10, "network": "solana"},
        {"address": B, "pnl": 5, "network": "solana"}]}}
    http = StubHTTP({"https://p/": birdeye_shaped})
    wallets, others, keys = probe("https://p/", {}, http)
    assert wallets == {"data.items[].address": 2}
    assert others == {} and keys == ["data", "success"]


def test_probe_ranks_the_richest_path_first():
    """A response can carry several address-shaped fields; the list is the one wanted."""
    from adapters.shortlist import probe
    payload = {"owner": A, "data": [{"wallet": B}, {"wallet": C}, {"wallet": A}]}
    wallets, _, _ = probe("https://p/", {}, StubHTTP({"https://p/": payload}))
    assert list(wallets)[0] == "data[].wallet"
    assert wallets["data[].wallet"] == 3 and wallets["owner"] == 1


def test_probe_separates_token_and_pool_addresses_from_wallets():
    """Measured against a real Birdeye trades response: a mint, a pool and a
    wallet are all 32-byte base58, and feeding a mint to the ranker is silent
    nonsense rather than an error."""
    from adapters.shortlist import probe
    trades_shaped = {"data": {"items": [
        {"owner": A, "poolId": B, "base": {"address": C}, "quote": {"address": B},
         "from": {"address": C}, "to": {"address": A}}]}}
    wallets, others, _ = probe("https://p/", {}, StubHTTP({"https://p/": trades_shaped}))
    assert list(wallets) == ["data.items[].owner"]
    assert set(others) == {"data.items[].poolId", "data.items[].base.address",
                           "data.items[].quote.address", "data.items[].from.address",
                           "data.items[].to.address"}


def test_probe_keeps_a_token_field_out_of_the_wallet_bucket():
    from adapters.shortlist import probe
    top_traders = {"data": {"items": [{"owner": A, "tokenAddress": B}]}}
    wallets, others, _ = probe("https://p/", {}, StubHTTP({"https://p/": top_traders}))
    assert list(wallets) == ["data.items[].owner"]
    assert list(others) == ["data.items[].tokenAddress"]


def test_probe_without_the_key_says_so_instead_of_calling(monkeypatch):
    from adapters.shortlist import probe
    monkeypatch.delenv("PROBE_KEY", raising=False)
    http = StubHTTP({})
    with pytest.raises(PermissionError, match="PROBE_KEY"):
        probe("https://p/", {"X-API-KEY": "env:PROBE_KEY"}, http)
    assert http.seen == [], "nothing is requested without the key"


def test_an_empty_source_list_explains_itself(tmp_path, monkeypatch):
    """A key in .env does nothing alone, and silence made that impossible to see."""
    from twobots.cli import refresh_shortlist
    from twobots.storage import Store
    monkeypatch.setenv("BIRDEYE_API_KEY", "set-but-unused")
    cfg = load_config(ROOT / "config.example.yaml")
    cfg["data_dir"] = str(tmp_path)
    cfg["shortlist"]["sources"] = []
    store = Store(tmp_path)
    try:
        result = refresh_shortlist(cfg, store, None)
        assert result["addresses"] == 0
        assert "No sources" in result["hint"]
        assert "BIRDEYE_API_KEY" in result["hint"]
    finally:
        store.close()


def test_invalid_addresses_are_counted_and_dropped():
    http = StubHTTP({"https://mixed/": {"data": [{"address": A}, {"address": "not-an-address"},
                                                 {"address": ""}]}})
    merged, report = collect([{"name": "mixed", "kind": "http", "url": "https://mixed/",
                               "address_path": "data[].address"}], http, now=100)
    assert set(merged) == {A}
    assert report[0] == {"source": "mixed", "returned": 3, "valid": 1, "invalid": 2}


@pytest.mark.parametrize("name,content,source", [
    ("list.csv", f"address,pnl\n{A},100\n{B},50\n", {"column": "address"}),
    ("list.json", json.dumps([A, B]), {}),
    ("list.txt", f"{A}\n{B}\n", {}),
    ("nested.json", json.dumps({"rows": [{"w": A}, {"w": B}]}), {"address_path": "rows[].w"}),
])
def test_exported_files_are_read_in_whatever_shape_they_arrive(tmp_path, name, content, source):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    merged, report = collect([{"name": "file", "kind": "file", "path": str(path), **source}],
                             None, now=100)
    assert set(merged) == {A, B}


def test_config_refuses_a_source_that_cannot_work(tmp_path):
    raw = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for broken, match in ((
            "shortlist:\n  enabled: true\n  sources:\n    - {kind: http, url: 'http://insecure/', address_path: a}\n",
            "HTTPS"),
            ("shortlist:\n  enabled: true\n  sources:\n    - {kind: http, url: 'https://ok/'}\n",
             "address_path"),
            ("shortlist:\n  enabled: true\n  sources:\n    - {kind: rpc, url: 'https://ok/'}\n",
             "http or file")):
        path = tmp_path / "c.yaml"
        path.write_text(raw.replace("shortlist:\n", broken + "_unused:\n", 1), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            load_config(path)


def test_shortlist_addresses_outrank_discovery_for_reconstruction(tmp_path):
    """Reconstruction is the scarce resource, so the better-justified list goes first."""
    from twobots.cli import candidates
    from twobots.storage import Store
    cfg = load_config(ROOT / "config.example.yaml")
    cfg["data_dir"] = str(tmp_path)
    cfg["wallets"]["ledger_dir"] = str(tmp_path / "ledgers")
    cfg["wallets"]["addresses"] = [C]
    store = Store(tmp_path)
    try:
        store.set("wallet:shortlist", {"at": 0, "addresses": [B]})
        store.set("wallet:discovery:solana", {"at": 0, "addresses": {A: 100}})
        assert candidates(cfg, store) == [C, B, A]
    finally:
        store.close()
