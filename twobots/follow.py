"""Copy trading from ranked wallets. Paper only: quotes are read-only, nothing is signed.

The scanner ranks historical wallet behaviour on a six-hour cadence; this module
consumes a separate near-real-time fill feed for the wallets that survived that
ranking. A leader's historical score is not evidence that copying it is
profitable: entry price, lag, size and capacity all differ from the leader's.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from urllib.parse import quote

from .dex import DexPaper, stressed_raw
from .net import auth_headers
from .wallets import BLOCKING_FLAGS, decimal, timestamp, valid_address

LOG = logging.getLogger(__name__)
MAX_FILLS = 500
# Sizing, capital and risk come from `follow`; venue physics stay with `dex`.
POLICY = ("initial_cash", "ticket_usd", "max_positions", "max_drawdown", "max_hold_hours",
          "stop_fraction", "trail_fraction", "poll_s", "mark_every_s")


class ActivityFeed:
    """Recent fills for one leader. Read-only; never requests a wallet signature."""

    def __init__(self, cfg, http):
        self.cfg, self.f, self.http = cfg, cfg["follow"], http
        self.network = cfg["scanner"]["network"]

    def read(self, address):
        cap = self.f["max_activity_mb"] * 1024 * 1024
        if self.f["source"] == "local":
            with (Path(self.f["activity_dir"]) / (address + ".json")).open("rb") as handle:
                raw = handle.read(cap + 1)
            if len(raw) > cap:
                raise ValueError("Leader activity exceeds size cap")
            return json.loads(raw.decode("utf-8-sig"))
        if not self.f["url_template"]:
            raise ValueError("Configure a trusted leader activity adapter URL")
        url = self.f["url_template"].format(address=quote(address, safe=""),
                                            network=quote(self.network, safe=""))
        return json.loads(self.http.request(url, headers=auth_headers(self.f), max_bytes=cap))

    def fills(self, address, now):
        data = self.read(address)
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1 or data.get("network") != self.network
                or data.get("address") != address):
            raise ValueError("Leader activity schema/network/address mismatch")
        asof = timestamp(data["asof"])
        if asof > now + 60 or now - asof > self.f["max_feed_age_s"]:
            raise ValueError("Leader activity feed is stale")
        rows = data.get("fills")
        if not isinstance(rows, list) or len(rows) > MAX_FILLS:
            raise ValueError(f"Expected at most {MAX_FILLS} recent leader fills")
        out, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Each leader fill must be an object")
            ident = row.get("id")
            if not isinstance(ident, str) or not ident or ident in seen:
                raise ValueError("Missing or duplicate leader fill id")
            seen.add(ident)
            if row.get("side") not in ("buy", "sell"):
                raise ValueError("Leader fill side must be buy or sell")
            if not valid_address(row.get("token")):
                raise ValueError("Invalid leader fill token mint")
            ts = timestamp(row["ts"])
            if ts > now + 60:
                raise ValueError("Leader fill timestamp is in the future")
            out.append({"id": ident, "ts": ts, "side": row["side"], "token": row["token"],
                        "notional_usd": float(decimal(row.get("notional_usd"), "notional_usd"))})
        out.sort(key=lambda r: (r["ts"], r["id"]))
        return out


class CopyTrader(DexPaper):
    """Mirrors ranked-wallet fills into a separate paper account."""

    venue = "copy"

    def __init__(self, cfg, store, gateway, feed):
        f = cfg["follow"]
        super().__init__(cfg, store, gateway, {**cfg["dex"], **{k: f[k] for k in POLICY}})
        self.f, self.feed, self.next_status = f, feed, 0.0

    def venue_fee(self, side, amount, quote):
        """Chain cost plus what a copy-trading service charges on the notional.

        Following through a platform costs a percentage of every entry and every
        exit, and that is precisely the cost that decides whether a thin edge
        survives. Simulating without it measures a strategy nobody can buy.
        """
        notional = self.usd(amount if side == "BUY" else quote["out_amount"])
        return super().venue_fee(side, amount, quote) + notional * self.f["platform_fee_fraction"]

    def leaders(self, now):
        report = self.store.get("wallet:latest")
        if not report or now - report["generated_at"] > self.f["ranking_max_age_s"]:
            return []
        chosen = []
        for row in report["ranking"]:
            if (row["score"] is None or row["score"] < self.f["min_score"]
                    or now - row["asof"] > self.f["ranking_max_age_s"]
                    or set(row["flags"]) - set(self.f["allowed_flags"])):
                continue
            chosen.append(row["address"])
            if len(chosen) >= self.f["max_leaders"]:
                break
        return chosen

    def signals(self, leaders, now):
        seen = self.store.get("copy:seen", {})
        fresh = []
        for address in leaders:
            try:
                rows = self.feed.fills(address, now)
            except FileNotFoundError:
                self.store.event("copy_activity_missing", {"leader": address})
                continue
            except Exception as exc:
                self.store.event("copy_activity_error", {"leader": address, "type": type(exc).__name__})
                continue
            for row in rows:
                uid = address + ":" + row["id"]
                if uid in seen:
                    continue
                # Marked seen even when too old, so a stale fill is never acted on later.
                seen[uid] = now
                if now - row["ts"] <= self.f["max_signal_age_s"]:
                    fresh.append({**row, "leader": address, "uid": uid, "lag_s": now - row["ts"]})
        if len(seen) > self.f["seen_memory"]:
            seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-self.f["seen_memory"]:])
        self.store.set("copy:seen", seen)
        fresh.sort(key=lambda r: (r["ts"], r["uid"]))
        return fresh

    async def entry(self, signal):
        token = signal["token"]
        amount = self.raw_usd(self.c["ticket_usd"])
        buy = await self.quote(self.c["quote_token"], token, amount)
        available = stressed_raw(buy["out_amount"], self.c["adverse_output_bps"])
        if available < buy["min_out"]:
            return {"status": "ROUNDTRIP_COST_REJECTED", "reason": "buy_stress_below_minimum"}
        sell = await self.quote(token, self.c["quote_token"], available)
        # The screen exists to refuse an illiquid token, so it measures what the
        # market costs: spread, impact and chain fees. The platform's percentage
        # applies to every token alike, and pre-filtering on it would refuse
        # everything and measure nothing. It is charged on the fill instead,
        # which is where it decides whether the leader's edge survived.
        total_fee = (DexPaper.venue_fee(self, "BUY", amount, buy)
                     + DexPaper.venue_fee(self, "SELL", available, sell))
        proceeds = self.usd(stressed_raw(sell["out_amount"], self.c["adverse_output_bps"]))
        roundtrip = (self.usd(amount) - proceeds + total_fee) / self.usd(amount)
        if roundtrip < 0 or roundtrip > self.c["max_roundtrip_cost_fraction"]:
            return {"status": "ROUNDTRIP_COST_REJECTED", "cost_fraction": roundtrip}
        # Quoting consumed time; re-check lag against the price actually obtainable now.
        lag = time.time() - signal["ts"]
        if lag > self.f["max_signal_age_s"]:
            return {"status": "COPY_LAG_EXCEEDED", "lag_s": lag}
        result = await self.swap(token, "BUY", amount, "copy:" + signal["leader"], buy)
        if result.get("settled_status") == "FILLED":
            state = self.ledger.state()
            position = state["positions"].get(token)
            if position is not None:
                position.update(leader=signal["leader"], signal_ts=signal["ts"],
                                copy_lag_s=time.time() - signal["ts"])
                self.store.set(self.ledger.key, state)
        return result

    def idle_reason(self, now):
        """Why there is nothing to copy, in the order the gates actually apply."""
        report = self.store.get("wallet:latest")
        if not report:
            return "scanner has not produced a ranking yet"
        if now - report["generated_at"] > self.f["ranking_max_age_s"]:
            return "ranking is stale"
        ranked = [r for r in report["ranking"] if r["score"] is not None]
        if not ranked:
            examined = report["ranking"]
            if not examined:
                return f"no ledger reconstructed yet ({len(report['unavailable'])} candidates waiting)"
            # Analysed and refused is a different situation from not yet analysed,
            # and only the first tells you which gate to look at.
            counts = {}
            for row in examined:
                for flag in row["flags"]:
                    if flag in BLOCKING_FLAGS:
                        counts[flag] = counts.get(flag, 0) + 1
            best = max(r["windows"]["90"]["closed_cycles"] for r in examined)
            detail = ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
            return (f"{len(examined)} analysed, none qualified ({detail}; best had {best} "
                    f"closed cycles), {len(report['unavailable'])} still without a ledger")
        if not [r for r in ranked if r["score"] >= self.f["min_score"]]:
            return f"best score {max(r['score'] for r in ranked):.1f} is below min_score {self.f['min_score']}"
        return "every ranked wallet carries a flag outside allowed_flags"

    async def step(self):
        now = time.time()
        self.store.set("heartbeat:copy", now)
        if now >= self.next_mark:
            await self.mark_and_exit()
            self.next_mark = now + self.c["mark_every_s"]
        leaders = self.leaders(now)
        self.store.set("copy:leaders", {"at": now, "addresses": leaders})
        state = self.ledger.state()
        if now >= self.next_status:
            # Silence is indistinguishable from a hang, so say what is happening
            # even when the answer is that nothing can be.
            if leaders:
                LOG.info("COPY paper equity %.4f, leaders=%d, positions=%d, halted=%s",
                         state["cash"] + sum(p.get("last_value", 0)
                                             for p in state["positions"].values()),
                         len(leaders), len(state["positions"]), state["halted"])
            else:
                LOG.info("COPY idle: %s", self.idle_reason(now))
            self.next_status = now + self.f["status_every_s"]
        if not leaders or state["halted"]:
            return
        bought = False
        for signal in self.signals(leaders, now):
            state = self.ledger.state()
            token = signal["token"]
            if signal["side"] == "sell":
                if self.f["mirror_exits"] and token in state["positions"]:
                    try:
                        await self.swap(token, "SELL", int(state["positions"][token]["raw_qty"]),
                                        "leader_exit:" + signal["leader"])
                    except Exception as exc:
                        # One unsellable token must not drop the remaining leader signals.
                        self.store.event("copy_exit_unavailable",
                                         {"token": token, "type": type(exc).__name__})
                continue
            # One entry per iteration; cash and inventory are re-read next cycle.
            if (bought or token in state["positions"]
                    or len(state["positions"]) >= self.c["max_positions"]
                    or signal["notional_usd"] < self.f["min_leader_notional_usd"]):
                continue
            key = "copy:attempt:" + token
            if now - self.store.get(key, 0) < self.f["cooldown_s"]:
                continue
            self.store.set(key, now)
            result = await self.entry(signal)
            self.store.event("copy_entry_result", {"leader": signal["leader"], "token": token,
                                                   "lag_s": round(signal["lag_s"], 3), **result})
            bought = True
        self.store.set("heartbeat:copy", time.time())
