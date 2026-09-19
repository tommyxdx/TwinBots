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


def chase_fraction(signal, spent_usd, quoted_out_raw):
    """How much more a follower pays per token than the leader did, as a fraction.

    On a thin pool the leader's own order is the price move: a single $34k buy
    lifted one token 4.67x in the seventy seconds before anyone could follow,
    and the follower bought the top of the leader's own impact. Measured in raw
    token units per dollar, so neither decimals nor float prices are involved
    and a token priced at 1e-9 compares exactly as one priced at 100.

    None when the leader's quantity is unknown.
    """
    quantity = signal.get("quantity_raw")
    if not quantity or signal["notional_usd"] <= 0 or quoted_out_raw <= 0 or spent_usd <= 0:
        return None
    return (quantity / signal["notional_usd"]) / (quoted_out_raw / spent_usd) - 1
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
            quantity = row.get("quantity_raw")
            if quantity is not None and (not isinstance(quantity, str) or not quantity.isdigit()
                                         or int(quantity) <= 0):
                raise ValueError("Leader fill quantity_raw must be a positive integer string")
            out.append({"id": ident, "ts": ts, "side": row["side"], "token": row["token"],
                        "notional_usd": float(decimal(row.get("notional_usd"), "notional_usd")),
                        "quantity_raw": int(quantity) if quantity is not None else None})
        out.sort(key=lambda r: (r["ts"], r["id"]))
        return out


class CopyTrader(DexPaper):
    """Mirrors ranked-wallet fills into a separate paper account."""

    venue = "copy"
    # The trading book enforces every rule. The shadow book overrides these.
    enforce_chase = True
    one_entry_per_step = True
    explains_idle = True

    def __init__(self, cfg, store, gateway, feed):
        super().__init__(cfg, store, gateway, self.policy(cfg))
        self.f, self.feed, self.next_status = cfg["follow"], feed, 0.0
        self.impact, self.impact_at = {}, 0.0

    def policy(self, cfg):
        f = cfg["follow"]
        return {**cfg["dex"], **{k: f[k] for k in POLICY}}

    def venue_fee(self, side, amount, quote):
        """Chain cost plus what a copy-trading service charges on the notional.

        Following through a platform costs a percentage of every entry and every
        exit, and that is precisely the cost that decides whether a thin edge
        survives. Simulating without it measures a strategy nobody can buy.
        """
        notional = self.usd(amount if side == "BUY" else quote["out_amount"])
        return super().venue_fee(side, amount, quote) + notional * self.f["platform_fee_fraction"]

    def retention(self, snapshots):
        """-> {address: (kept, seen)} across the most recent ranking snapshots.

        A wallet that tops one ranking and is gone from the next is the shape a
        lucky streak makes. Surviving several independent rankings is the
        cheapest evidence of the opposite, and it costs nothing: the snapshots
        are already kept for the forward test.

        `seen` counts the rankings that analysed the wallet at all, so a wallet
        that has only just been reconstructed has nothing held against it. Only
        a wallet with a real record of dropping out is refused.
        """
        stamps = [r["ts"] for r in self.store.rows(
            "SELECT DISTINCT ts FROM rankings ORDER BY ts DESC LIMIT ?", (snapshots,))]
        if not stamps:
            return {}
        marks = ",".join("?" * len(stamps))
        rows = self.store.rows(
            "SELECT address, COUNT(*) AS seen, SUM(rank IS NOT NULL) AS kept "
            f"FROM rankings WHERE ts IN ({marks}) GROUP BY address", stamps)
        return {r["address"]: (r["kept"] or 0, r["seen"]) for r in rows}

    def impact_record(self, now):
        """-> {leader: [chase, ...]}, one observation per signal, recent window.

        Both books see the same signals and each records its own attempt, so an
        observation is keyed by the signal's uid and counted once. Cached for
        five minutes: selection runs every step and this reads the event log.
        """
        if now - self.impact_at < 300:
            return self.impact
        since = now - self.f["chase_window_days"] * 86400
        seen, record = set(), {}
        for row in self.store.rows(
                "SELECT payload FROM events WHERE kind IN ('copy_entry_result','shadow_entry_result') "
                "AND ts >= ?", (since,)):
            item = json.loads(row["payload"])
            uid, chase = item.get("uid"), item.get("chase")
            if uid is None or chase is None or uid in seen:
                continue
            seen.add(uid)
            record.setdefault(item["leader"], []).append(chase)
        self.impact, self.impact_at = record, now
        return record

    def unfollowable(self, row, now, held, impact=None):
        """Why this ranked wallet cannot be followed, or None if it can."""
        if row["score"] is None or row["score"] < self.f["min_score"]:
            return "below_min_score"
        if now - row["asof"] > self.f["ranking_max_age_s"]:
            return "ledger_stale"
        if set(row["flags"]) - set(self.f["allowed_flags"]):
            return "flagged"
        # A 90-day record says a wallet could trade, not that it still does.
        # Following one that has gone quiet produces no signals at all, and it
        # holds a leader slot that an active wallet would have used.
        recent = (row.get("windows") or {}).get("7") or {}
        if (recent.get("trade_fills") or 0) < self.f["min_recent_fills"]:
            return "dormant"
        # A leader whose recent signals were mostly priced several times over by
        # the time anyone could follow is making its money by moving the pool.
        # That profit is real and belongs to whoever moves first with size; a
        # small, late follower is the counterparty to it, not a share in it.
        chases = (impact or {}).get(row["address"]) or []
        limit = self.f["max_chase_fraction"]
        if limit and len(chases) >= self.f["min_chase_samples"]:
            if sorted(chases)[len(chases) // 2] > limit:
                return "impact_trader"
        kept, seen = held.get(row["address"], (0, 0))
        if seen >= self.f["min_retention_snapshots"] and kept / seen < self.f["min_retention"]:
            return "not_retained"
        return None

    def leaders(self, now):
        self.skipped = {}
        report = self.store.get("wallet:latest")
        if not report or now - report["generated_at"] > self.f["ranking_max_age_s"]:
            return []
        held = self.retention(self.f["retention_snapshots"])
        impact = self.impact_record(now)
        chosen = []
        for row in report["ranking"]:
            why = self.unfollowable(row, now, held, impact)
            if why:
                self.skipped[why] = self.skipped.get(why, 0) + 1
                continue
            chosen.append(row["address"])
            if len(chosen) >= self.f["max_leaders"]:
                break
        return chosen

    def leader_budget(self, leaders):
        """USD one leader may have at risk at once.

        Raising `max_leaders` is what lifts the signal rate; this is what keeps
        one hyperactive wallet from spending the whole book before the quieter
        leaders are heard from at all.

        Zero splits the book between the leaders there actually are, not the
        most there could be: the cap exists to protect the other leaders' share,
        so with one leader there is nobody to protect and no reason to leave
        most of the book idle. Set `leader_share` to pin it instead.
        """
        share = self.f["leader_share"] or 1 / max(1, len(leaders))
        return self.f["initial_cash"] * share

    def leader_exposure(self, state, leader):
        return sum(p.get("last_value", 0) or 0 for p in state["positions"].values()
                   if p.get("leader") == leader)

    def signals(self, leaders, now):
        seen = self.store.get(self.venue + ":seen", {})
        fresh = []
        for address in leaders:
            try:
                rows = self.feed.fills(address, now)
            except FileNotFoundError:
                self.store.event(self.venue + "_activity_missing", {"leader": address})
                continue
            except Exception as exc:
                self.store.event(self.venue + "_activity_error",
                                 {"leader": address, "type": type(exc).__name__})
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
        self.store.set(self.venue + ":seen", seen)
        fresh.sort(key=lambda r: (r["ts"], r["uid"]))
        return fresh

    async def entry(self, signal):
        token = signal["token"]
        amount = self.raw_usd(self.c["ticket_usd"])
        buy = await self.quote(self.c["quote_token"], token, amount)
        chase = chase_fraction(signal, self.usd(amount), buy["out_amount"])
        noted = {"chase": None if chase is None else round(chase, 4)}
        refused = self.chase_refusal(chase)
        if refused:
            return {**noted, "status": refused}
        available = stressed_raw(buy["out_amount"], self.c["adverse_output_bps"])
        if available < buy["min_out"]:
            return {**noted, "status": "ROUNDTRIP_COST_REJECTED", "reason": "buy_stress_below_minimum"}
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
            return {**noted, "status": "ROUNDTRIP_COST_REJECTED", "cost_fraction": roundtrip}
        # Quoting consumed time; re-check lag against the price actually obtainable now.
        lag = time.time() - signal["ts"]
        if lag > self.f["max_signal_age_s"]:
            return {**noted, "status": "COPY_LAG_EXCEEDED", "lag_s": lag}
        result = await self.swap(token, "BUY", amount, "copy:" + signal["leader"], buy,
                                 meta={"leader": signal["leader"], "chase": noted["chase"],
                                       "signal_uid": signal["uid"]})
        if result.get("settled_status") == "FILLED":
            state = self.ledger.state()
            position = state["positions"].get(token)
            if position is not None:
                position.update(leader=signal["leader"], signal_ts=signal["ts"],
                                copy_lag_s=time.time() - signal["ts"])
                self.store.set(self.ledger.key, state)
        return {**noted, **result}

    def chase_refusal(self, chase):
        """The status that refuses an entry on a move already made, or None."""
        limit = self.f["max_chase_fraction"]
        if not limit or not self.enforce_chase:
            return None
        if chase is None:
            # A feed that cannot say what the leader paid cannot show the move
            # is still ahead of us rather than already behind.
            return "CHASE_UNVERIFIABLE"
        return "CHASE_REJECTED" if chase > limit else None

    def status_line(self, state, leaders):
        equity = state["cash"] + sum(p.get("last_value", 0) for p in state["positions"].values())
        return ("COPY paper equity %.4f, leaders=%d, positions=%d, halted=%s"
                % (equity, len(leaders), len(state["positions"]), state["halted"]))

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
        skipped = getattr(self, "skipped", {})
        if skipped:
            return "no wallet passed leader selection (" + ", ".join(
                f"{k} x{v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) + ")"
        return "every ranked wallet carries a flag outside allowed_flags"

    async def step(self):
        now = time.time()
        self.store.set("heartbeat:" + self.venue, now)
        if now >= self.next_mark:
            await self.mark_and_exit()
            self.next_mark = now + self.c["mark_every_s"]
        leaders = self.leaders(now)
        self.store.set(self.venue + ":leaders", {"at": now, "addresses": leaders})
        state = self.ledger.state()
        if now >= self.next_status:
            # Silence is indistinguishable from a hang, so say what is happening
            # even when the answer is that nothing can be.
            if leaders:
                LOG.info(self.status_line(state, leaders))
            elif self.explains_idle:
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
                        self.store.event(self.venue + "_exit_unavailable",
                                         {"token": token, "type": type(exc).__name__})
                continue
            # One entry per iteration; cash and inventory are re-read next cycle.
            if (bought or token in state["positions"]
                    or len(state["positions"]) >= self.c["max_positions"]
                    or signal["notional_usd"] < self.f["min_leader_notional_usd"]):
                continue
            # Each leader gets its own slice of the book, so a burst from one
            # wallet cannot crowd out every other leader's signals.
            if (self.leader_exposure(state, signal["leader"]) + self.c["ticket_usd"]
                    > self.leader_budget(leaders)):
                self.store.event(self.venue + "_leader_budget_full",
                                 {"leader": signal["leader"], "token": token})
                continue
            key = self.venue + ":attempt:" + token
            if now - self.store.get(key, 0) < self.f["cooldown_s"]:
                continue
            self.store.set(key, now)
            result = await self.entry(signal)
            self.store.event(self.venue + "_entry_result",
                             {"leader": signal["leader"], "token": token, "uid": signal["uid"],
                              "signal_ts": signal["ts"], "lag_s": round(signal["lag_s"], 3),
                              **result})
            bought = self.one_entry_per_step
        self.store.set("heartbeat:" + self.venue, time.time())


class ShadowTrader(CopyTrader):
    """Takes every signal the trading book is offered, with none of its limits.

    The trading book answers what this policy would have made. This one answers
    what following the leaders would have made at all, and keeps answering
    while the trading book is halted: the first halt on the live box stopped
    all measurement for twelve hours. It is also the only way to price a gate,
    since nobody otherwise takes the trades a gate refuses -- the chase is
    recorded here on every fill, so its effect can be read straight off.

    Same quotes, latency, stress and exits as the trading book; only the limits
    differ. Its requests draw on a separate allowance, so it can never exhaust
    the budget the trading book needs to value its own positions.
    """

    venue = "shadow"
    enforce_chase = False
    one_entry_per_step = False
    explains_idle = False

    def policy(self, cfg):
        c = super().policy(cfg)
        # Cash, position count and drawdown never bind: every signal is taken.
        c.update(initial_cash=c["ticket_usd"] * 10000, max_positions=10 ** 6, max_drawdown=1.0)
        return c

    def leader_budget(self, leaders):
        return float("inf")

    def status_line(self, state, leaders):
        return ("SHADOW realized %+.2f USD, positions=%d (every signal, no limits)"
                % (state["realized_pnl"], len(state["positions"])))
