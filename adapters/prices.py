"""USD prices for the quote assets only, at execution time.

Research tokens are never priced here: their USD value is derived from the quote
asset actually exchanged, so a thin or manipulated memecoin oracle cannot inflate
a wallet's record. Only SOL needs a real series; USDC and USDT are held at 1.0.

SOL comes from Binance's public kline data, which needs no API key: monthly
archives for finished months, daily archives for the running one, and the REST
endpoint for today, because an archive exists only once its period has ended.
One-minute closes are used, so an execution is priced at the close of the minute
it landed in, not at its actual fill price.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .reconstruct import SOL_MINT, STABLE_MINTS

ARCHIVE = "https://data.binance.vision/data/spot"
REST = "https://api.binance.com/api/v3/klines"
ONE = Decimal(1)


def months_covering(start_ts, end_ts):
    start = datetime.fromtimestamp(start_ts, timezone.utc)
    end = datetime.fromtimestamp(end_ts, timezone.utc)
    months, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def fetch(url, cap=256 * 1024 * 1024, missing_ok=False):
    """Missing archives are normal at the edges of the published range."""
    request = urllib.request.Request(url, headers={"User-Agent": "TwinCryptoBots-Adapter/1.2.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(cap + 1)
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise
    if len(raw) > cap:
        raise ValueError("Kline archive exceeds size limit: " + url)
    return raw


class SolPrice:
    """Minute closes for SOLUSDT, cached on disk and verified against Binance's SHA256."""

    def __init__(self, cache_dir, symbol="SOLUSDT", interval="1m"):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.symbol, self.interval = symbol, interval
        self.times, self.closes, self.loaded, self.missing = [], [], set(), []

    def _archive(self, kind, key):
        """One monthly or daily kline file, cached and checksum-verified. None if absent."""
        name = f"{self.symbol}-{self.interval}-{key}.zip"
        path = self.cache / name
        url = f"{ARCHIVE}/{kind}/klines/{self.symbol}/{self.interval}/{name}"
        if path.exists():
            raw = path.read_bytes()
        else:
            raw = fetch(url, missing_ok=True)
            if raw is None:
                return None
            expected = fetch(url + ".CHECKSUM").decode().split()[0]
            if hashlib.sha256(raw).hexdigest().lower() != expected.lower():
                raise ValueError("Remote SHA256 mismatch: " + name)
            temp = path.with_suffix(".part")
            temp.write_bytes(raw)
            temp.replace(path)
        points = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise ValueError("Unexpected archive contents: " + name)
            with archive.open(names[0]) as handle:
                for row in csv.reader(io.TextIOWrapper(handle)):
                    if len(row) < 5 or not row[0].strip().isdigit():
                        continue
                    stamp = int(row[0])
                    # Binance switched open_time from milliseconds to microseconds.
                    while stamp > 1e11:
                        stamp //= 1000
                    points[stamp] = Decimal(row[4])
        return points

    def _today(self, end_ts):
        """Today has no archive of either kind; the REST endpoint is the only source."""
        midnight = datetime.fromtimestamp(end_ts, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        cursor, stop, points = int(midnight.timestamp() * 1000), int(end_ts * 1000), {}
        while cursor <= stop:
            query = urllib.parse.urlencode({"symbol": self.symbol, "interval": self.interval,
                                            "startTime": cursor, "limit": 1000})
            rows = json.loads(fetch(f"{REST}?{query}", cap=8 * 1024 * 1024))
            if not rows:
                break
            for row in rows:
                points[int(row[0]) // 1000] = Decimal(row[4])
            nxt = int(rows[-1][0]) + 60_000
            if nxt <= cursor:
                break
            cursor = nxt
        return points

    def load(self, start_ts, end_ts):
        """Monthly archives for finished months, daily for this month, REST for today.

        Binance publishes a monthly file only once the month is over and a daily
        file only once the day is, so asking for either at the live edge is a 404,
        not an outage. A genuinely missing span is left as a hole; `at` refuses to
        price a transaction that lands in one rather than reaching for a stale bar.
        """
        points = dict(zip(self.times, self.closes))
        today = datetime.fromtimestamp(end_ts, timezone.utc).date()
        for month in months_covering(start_ts, end_ts):
            if month in self.loaded or month == today.strftime("%Y-%m"):
                continue
            found = self._archive("monthly", month)
            if found is None:
                self.missing.append(month)
                continue
            points.update(found)
            self.loaded.add(month)
        day = max(datetime.fromtimestamp(start_ts, timezone.utc).date(), today.replace(day=1))
        while day < today:
            key = day.isoformat()
            if key not in self.loaded:
                found = self._archive("daily", key)
                if found is None:
                    self.missing.append(key)
                else:
                    points.update(found)
                    self.loaded.add(key)
            day += timedelta(days=1)
        if end_ts >= datetime.combine(today, datetime.min.time(),
                                      tzinfo=timezone.utc).timestamp():
            points.update(self._today(end_ts))
        self.times = sorted(points)
        self.closes = [points[t] for t in self.times]
        return len(self.times)

    def at(self, ts):
        if not self.times:
            raise ValueError("Load the SOL price series before pricing transactions")
        index = bisect.bisect_right(self.times, ts) - 1
        if index < 0:
            raise ValueError(f"No SOL price at or before {ts}; widen the loaded range")
        # A long gap means the series does not actually cover this execution.
        if ts - self.times[index] > 3600:
            raise ValueError(f"SOL price gap of {ts - self.times[index]}s at {ts}")
        return self.closes[index]


def quote_pricer(sol_price):
    """price_usd(mint, ts) for the quote assets the reconstructor can encounter."""
    def price_usd(mint, ts):
        if mint in STABLE_MINTS:
            return ONE
        if mint == SOL_MINT:
            return sol_price.at(ts)
        raise ValueError("No USD price source for quote asset " + mint)
    return price_usd
