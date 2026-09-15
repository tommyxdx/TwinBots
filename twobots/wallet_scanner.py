"""Bounded candidate discovery + cached, read-only normalized wallet ledgers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

from .net import auth_headers
from .wallets import analyze_ledger, rank_wallets, valid_address


class WalletScanner:
    def __init__(self, cfg, store, http, fetcher, telegram=None):
        self.cfg, self.c, self.store, self.http = cfg, cfg["wallets"], store, http
        self.fetcher = fetcher
        self.network = cfg["scanner"]["network"]
        identity = json.dumps({k: self.c[k] for k in ("source", "ledger_dir", "url_template", "api_key_env")}, sort_keys=True)
        self.namespace = hashlib.sha256((self.network + identity).encode()).hexdigest()[:20]

    def key(self, address):
        return f"wallet:{self.namespace}:{address}"

    def discover(self, now):
        key = f"wallet:discovery:{self.network}"
        state = self.store.get(key, {"at": 0, "addresses": {}})
        addresses = {a: ts for a, ts in state["addresses"].items() if now - ts <= 30 * 86400}
        if not self.c["discover_enabled"] or now - state["at"] < self.c["discovery_every_s"]:
            return addresses
        # Record attempts as well as successes, preventing expensive error loops.
        self.store.set(key, {"at": now, "addresses": addresses})
        try:
            pools = self.fetcher.discover()[:self.c["discovery_max_pools"]]
            for pool in pools:
                url = (self.cfg["scanner"]["gecko_base"].rstrip("/") + "/networks/"
                       + quote(self.network, safe="") + "/pools/" + quote(pool["pool"], safe="") + "/trades")
                data = self.http.json(url, headers={"Accept": "application/json;version=20230203"})
                found = set()
                for trade in data.get("data", []):
                    address = trade.get("attributes", {}).get("tx_from_address")
                    if valid_address(address):
                        addresses[address] = now
                        found.add(address)
                        if len(found) >= self.c["discovery_addresses_per_pool"]:
                            break
        except Exception as exc:
            self.store.event("wallet_discovery_error", {"type": type(exc).__name__})
        addresses = dict(sorted(addresses.items(), key=lambda x: (-x[1], x[0]))[:self.c["max_candidates"]])
        self.store.set(key, {"at": now, "addresses": addresses})
        return addresses

    def read_ledger(self, address):
        if self.c["source"] in ("chain", "local"):
            path = Path(self.c["ledger_dir"]) / (address + ".json")
            with path.open("rb") as handle:
                raw = handle.read(self.c["max_ledger_mb"] * 1024 * 1024 + 1)
            if len(raw) > self.c["max_ledger_mb"] * 1024 * 1024:
                raise ValueError("Wallet ledger exceeds size cap")
            return json.loads(raw.decode("utf-8-sig"))
        if not self.c["url_template"]:
            raise ValueError("Configure a trusted normalized ledger adapter URL")
        url = self.c["url_template"].format(address=quote(address, safe=""), network=quote(self.network, safe=""))
        raw = self.http.request(url, headers=auth_headers(self.c),
                                max_bytes=self.c["max_ledger_mb"] * 1024 * 1024)
        return json.loads(raw)

    def run_once(self):
        now = time.time()
        discovered = self.discover(now)
        local = []
        if self.c["source"] in ("chain", "local"):
            folder = Path(self.c["ledger_dir"])
            if folder.is_dir():
                local = sorted(p.stem for p in folder.glob("*.json") if valid_address(p.stem))[:self.c["max_candidates"]]
        addresses = list(dict.fromkeys(self.c["addresses"] + local + list(discovered)))[:self.c["max_candidates"]]
        states = {a: self.store.get(self.key(a), {}) for a in addresses}
        due = [a for a in addresses if now - states[a].get("attempted_at", 0) >= self.c["refresh_s"]]
        # Reading local files consumes no API, so edits and freshly built
        # ledgers are picked up on the next cycle instead of on a timer.
        if self.c["source"] in ("chain", "local"):
            due = list(addresses)
        due.sort(key=lambda a: (states[a].get("attempted_at", 0), a))
        for address in due[:self.c["max_wallets_per_run"]]:
            state = {"attempted_at": now}
            try:
                ledger = self.read_ledger(address)
                analyze_ledger(ledger, address, self.network, now, self.c["max_age_s"],
                               self.c["min_history_days"])
                state["ledger"] = ledger
            except FileNotFoundError:
                state["error"] = "No local ledger; supply complete historical data for this address"
            except (ValueError, KeyError, TypeError) as exc:
                # Schema error messages contain field names, never raw payloads/credentials.
                state["error"] = str(exc) if isinstance(exc, ValueError) else "Malformed wallet ledger"
                if isinstance(exc, json.JSONDecodeError):
                    state["error"] = "Invalid wallet JSON"
            except Exception as exc:
                state["error"] = f"Wallet data unavailable ({type(exc).__name__})"
            # Invalid refreshes remove the old good result instead of hiding bad news.
            states[address] = state
            self.store.set(self.key(address), state)
        analyses, unavailable = [], []
        for address in addresses:
            state = states[address]
            if "ledger" not in state:
                unavailable.append({"address": address, "status": "unavailable",
                                    "reason": state.get("error", "Pending bounded refresh")})
                continue
            try:
                analyses.append(analyze_ledger(state["ledger"], address, self.network, now,
                                               self.c["max_age_s"], self.c["min_history_days"]))
            except (ValueError, KeyError, TypeError) as exc:
                unavailable.append({"address": address, "status": "unavailable", "reason": str(exc)})
        result = {"generated_at": now, "network": self.network, "kind": "wallets",
                  "source_mode": self.c["source"], "candidate_count": len(addresses),
                  "ranking": rank_wallets(analyses, self.c["min_closed_cycles"], self.c["min_closed_tokens"],
                                          self.c["max_censored_cost_fraction"]),
                  "unavailable": unavailable,
                  "note": "Historical research only. Provider coverage is not independently verified. A rank is not evidence that copying the wallet is profitable.",
                  "discovery_note": "Recent pool senders are candidates, not verified beneficial owners or a full-chain sample"}
        self.store.set("wallet:latest", result)
        self.store.set("heartbeat:scanner", now)
        return result
