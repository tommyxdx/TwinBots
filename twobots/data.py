from __future__ import annotations
import csv
import hashlib
import io
import json
import logging
import math
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from .net import auth_headers
from .storage import dumps

LOG = logging.getLogger(__name__)
INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def seconds(value):
    if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    x = float(value)
    if x > 1e14:
        return x / 1e6
    if x > 1e11:
        return x / 1e3
    return x


def number(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default


def parse_klines(raw, now=None):
    result = []
    now = now or time.time()
    for r in raw:
        if len(r) < 8 or number(r[0]) is None:
            continue
        t, end = int(seconds(r[0])), int(seconds(r[6]))
        if end >= now:
            continue
        o, h, low, c, v, qv = map(float, (r[1], r[2], r[3], r[4], r[5], r[7]))
        if not all(math.isfinite(x) for x in (o, h, low, c, v, qv)) or min(o, h, low, c) <= 0:
            continue
        if low > min(o, c) or h < max(o, c) or v < 0 or qv < 0:
            continue
        result.append((t, end, o, h, low, c, v, qv))
    return result


def months_before(n, now=None):
    dt = now or datetime.now(timezone.utc)
    year, month = dt.year, dt.month
    result = []
    for _ in range(n):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        result.append(f"{year:04d}-{month:02d}")
    return list(reversed(result))


class Fetcher:
    def __init__(self, cfg, store, http):
        self.cfg, self.store, self.http = cfg, store, http
        self.cache = store.root / "downloads"
        self.cache.mkdir(exist_ok=True)

    def binance_archive(self, symbol, month):
        cfg = self.cfg["history"]
        interval = cfg["interval"]
        name = f"{symbol}-{interval}-{month}.zip"
        path = self.cache / name
        url = f"{cfg['archive_base'].rstrip('/')}/data/spot/monthly/klines/{symbol}/{interval}/{name}"
        if path.exists():
            raw = path.read_bytes()
            manifest = self.store.get("archive:" + name)
            if not manifest or hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
                raise ValueError(f"Local archive checksum mismatch: {name}; remove it and refetch")
        else:
            raw = self.http.request(url)
            digest = hashlib.sha256(raw).hexdigest()
            if cfg["verify_sha256"]:
                check = self.http.request(url + ".CHECKSUM").decode().split()[0]
                if digest.lower() != check.lower():
                    raise ValueError(f"Remote SHA256 mismatch: {name}")
            temp = path.with_suffix(".part")
            temp.write_bytes(raw)
            temp.replace(path)
            self.store.set("archive:" + name, {"url": url, "sha256": digest, "fetched_at": time.time()})
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [x for x in z.namelist() if x.endswith(".csv")]
            if len(names) != 1 or z.getinfo(names[0]).file_size > self.cfg["http"]["max_download_mb"] * 1024**2:
                raise ValueError("Unexpected archive contents/size")
            with z.open(names[0]) as f:
                rows = parse_klines(csv.reader(io.TextIOWrapper(f)))
        self.store.add_candles("cex", symbol, rows)
        return len(rows)

    def recent_cex(self, symbol, limit=500):
        c = self.cfg["cex"]
        raw = self.http.json(c["rest_base"].rstrip("/") + "/api/v3/klines",
                             {"symbol": symbol, "interval": c["interval"], "limit": limit})
        rows = parse_klines(raw)
        self.store.add_candles("cex", symbol, rows)
        return rows

    def bootstrap(self, force=False):
        h = self.cfg["history"]
        fingerprint = hashlib.sha256(dumps(h).encode()).hexdigest()[:12]
        key = "bootstrap:" + fingerprint
        last = self.store.get(key, 0)
        if not force and time.time() - last < h["refresh_after_hours"] * 3600:
            return {"cached": True}
        report = {"archives": [], "errors": [], "source": "Binance public monthly archives"}
        for symbol in h["symbols"]:
            for month in months_before(h["months"]):
                try:
                    n = self.binance_archive(symbol, month)
                    report["archives"].append({"symbol": symbol, "month": month, "rows": n})
                    LOG.info("History %s %s: %d bars", symbol, month, n)
                except Exception as exc:
                    LOG.warning("History %s %s unavailable: %s", symbol, month, exc)
                    report["errors"].append({"symbol": symbol, "month": month, "error": str(exc)})
            try:
                self.recent_cex(symbol, 1000)
            except Exception as exc:
                report["errors"].append({"symbol": symbol, "stage": "recent", "error": str(exc)})
        token_mode = self.cfg["scanner"].get("kind", "wallets") == "tokens"
        if token_mode and h.get("pool_catalog_url"):
            try:
                self.import_catalog(h["pool_catalog_url"])
            except Exception as exc:
                report["errors"].append({"stage": "catalog", "error": str(exc)})
        if token_mode and h.get("scanner_dataset_url"):
            try:
                self.download_scanner_dataset()
            except Exception as exc:
                report["errors"].append({"stage": "scanner_dataset", "error": str(exc)})
        # Cache successful imports individually; retry partial bootstrap on next startup.
        if not report["errors"]:
            self.store.set(key, time.time())
        self.store.set("bootstrap:report", report)
        return report

    def download_scanner_dataset(self):
        h = self.cfg["history"]
        headers = auth_headers(h, "dataset_")
        raw = self.http.request(h["scanner_dataset_url"], headers=headers)
        digest = hashlib.sha256(raw).hexdigest()
        if h.get("scanner_dataset_sha256") and h["scanner_dataset_sha256"] != digest:
            raise ValueError("Scanner dataset SHA256 mismatch")
        if raw[:1] == b"\x80":
            raise ValueError("Remote pickle is not supported. Use documented CSV, not executable serialization.")
        text = raw.decode("utf-8-sig")
        fields = csv.DictReader(io.StringIO(text)).fieldnames or []
        if not {"network", "token", "asof", "label_known_at", "target", "label"}.issubset(fields):
            raise ValueError("Scanner CSV missing required PIT/label columns; see docs/DATA.md")
        path = self.cache / "scanner_dataset.csv"
        path.write_text(text, encoding="utf-8")
        self.store.set("scanner:dataset", {"sha256": digest, "fetched_at": time.time(), "columns": fields})
        return str(path)

    def import_catalog(self, url):
        raw = self.http.request(url, headers=auth_headers(self.cfg["history"], "dataset_"))
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if not {"network", "pool", "token", "created_at"}.issubset(reader.fieldnames or []):
            raise ValueError("Pool catalog CSV requires network,pool,token,created_at")
        n = 0
        with self.store.transaction() as db:
            for r in reader:
                created = seconds(r["created_at"])
                db.execute("INSERT OR IGNORE INTO pools VALUES(?,?,?,?,?,?,?)",
                           (r["network"], r["pool"], r["token"], created, time.time(), 0, dumps({"catalog": True})))
                n += 1
        return n

    def discover(self):
        cfg = self.cfg["scanner"]
        found = []
        for page in range(1, cfg["pages"] + 1):
            payload = self.http.json(cfg["gecko_base"] + f"/networks/{quote(cfg['network'], safe='')}/new_pools",
                                     {"page": page, "include": "base_token"})
            included = {x["id"]: x.get("attributes", {}) for x in payload.get("included", [])}
            for p in payload.get("data", []):
                a = p["attributes"]
                rel = p.get("relationships", {}).get("base_token", {}).get("data", {})
                token = rel.get("id", "").removeprefix(cfg["network"] + "_")
                if not token:
                    continue
                created = seconds(a["pool_created_at"])
                item = {"network": cfg["network"], "pool": a["address"], "token": token,
                        "created": created, "observed_at": time.time(), "attributes": a,
                        "token_attributes": included.get(rel.get("id"), {}), "source": "geckoterminal_new_pools"}
                self.save_pool(item)
                found.append(item)
        # This is the provider's visible recent-pool universe, NOT a complete launch feed.
        self.store.set("scanner:discovery", {"at": time.time(), "count": len(found), "coverage": "provider_recent_pools"})
        return found

    def save_pool(self, item):
        with self.store.transaction() as db:
            db.execute("""INSERT INTO pools VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(network,pool) DO UPDATE SET last_seen=excluded.last_seen,payload=excluded.payload""",
                       (item["network"], item["pool"], item["token"], item["created"], time.time(),
                        time.time(), dumps(item)))

    def pool_history(self, network, pool, pages=1, refresh=False):
        key = f"pool_history:{network}:{pool}"
        if not refresh and time.time() - self.store.get(key, 0) < 3600:
            return 0
        c = self.cfg["scanner"]
        total, before = 0, None
        for _ in range(pages):
            params = {"aggregate": 5, "limit": c["history_limit"], "currency": "usd",
                      "token": "base", "include_empty_intervals": "false"}
            if before:
                params["before_timestamp"] = before
            url = c["gecko_base"] + f"/networks/{quote(network,safe='')}/pools/{quote(pool,safe='')}/ohlcv/minute"
            payload = self.http.json(url, params)
            raw = payload.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            rows = []
            for ts, o, h, low, close, volume in raw:
                if not all(number(x) is not None for x in (ts, o, h, low, close, volume)):
                    continue
                ts, o, h, low, close, volume = map(float, (ts, o, h, low, close, volume))
                if ts != int(ts) or int(ts) % 300 or volume < 0:
                    continue
                if ts + 300 >= time.time():
                    continue
                if min(o, h, low, close) <= 0 or low > min(o, close) or h < max(o, close):
                    continue
                rows.append((int(ts), int(ts + 299), o, h, low, close, volume, volume))
            self.store.add_candles("dex:" + network, pool, rows)
            total += len(rows)
            if not rows:
                break
            oldest = min(r[0] for r in rows)
            if before is not None and oldest >= before:
                break
            before = oldest - 1
        self.store.set(key, time.time())
        return total

    def follow_cohort(self, pages=1):
        """Small prospectively selected cohort, not a winners-only history import."""
        h, now = self.cfg["history"], time.time()
        candidates = self.store.rows("SELECT * FROM pools WHERE network=? AND created>? ORDER BY first_seen,pool",
            (self.cfg["scanner"]["network"], now-h["cohort_follow_days"]*86400))
        cohort = self.store.get("history:cohort", [])
        valid = {p["pool"]:p for p in candidates}
        cohort = [k for k in cohort if k in valid]
        for p in candidates:
            # Only enroll near creation unless explicitly supplied in a historical catalog.
            payload = json.loads(p["payload"])
            prospectively_seen = p["first_seen"]-p["created"] <= 48*3600
            fraction = int(hashlib.sha256(p["pool"].encode()).hexdigest()[:8],16)/2**32
            if (p["pool"] not in cohort and len(cohort)<h["cohort_max_pools"] and
                    (prospectively_seen or payload.get("catalog")) and fraction<h["cohort_sample_fraction"]):
                cohort.append(p["pool"])
        self.store.set("history:cohort",cohort)
        report = {"tracked":len(cohort),"fetched":0,"errors":[],"coverage":"small sampled visible-pool cohort"}
        for pool in cohort:
            p = valid[pool]
            last = self.store.get(f"pool_history:{p['network']}:{pool}",0)
            if now-last < h["cohort_refresh_hours"]*3600:
                continue
            try:
                report["fetched"] += self.pool_history(p["network"],pool,pages,refresh=True)
            except Exception as exc:
                report["errors"].append({"pool":pool,"error":str(exc)})
        self.store.set("history:cohort_report",report)
        return report
