from __future__ import annotations
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def dumps(x):
    return json.dumps(x, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite3", timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.verify()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts REAL,kind TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS candles(market TEXT,symbol TEXT,ts INTEGER,close_ts INTEGER,
          o REAL,h REAL,l REAL,c REAL,v REAL,qv REAL,PRIMARY KEY(market,symbol,ts));
        CREATE TABLE IF NOT EXISTS pools(network TEXT,pool TEXT,token TEXT,created REAL,
          first_seen REAL,last_seen REAL,payload TEXT,PRIMARY KEY(network,pool));
        CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY,ts REAL,network TEXT,token TEXT,
          pool TEXT,score REAL,payload TEXT);
        CREATE INDEX IF NOT EXISTS scans_token ON scans(network,token,ts);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,venue TEXT,symbol TEXT,
          side TEXT,status TEXT,created REAL,payload TEXT);
        CREATE TABLE IF NOT EXISTS marks(id INTEGER PRIMARY KEY,ts REAL,venue TEXT,equity REAL,payload TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,dedupe TEXT UNIQUE,ts REAL,
          message TEXT,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(day TEXT PRIMARY KEY,n INTEGER NOT NULL);
        -- Ranking history. Forward performance cannot be measured against a
        -- ranking that was overwritten, and it cannot be backfilled later.
        CREATE TABLE IF NOT EXISTS rankings(ts REAL,address TEXT,rank INTEGER,score REAL,
          cycles INTEGER,roi7 REAL,roi30 REAL,roi90 REAL,censored REAL,flags TEXT,
          PRIMARY KEY(ts,address));
        """)
        self.db.commit()

    def verify(self):
        """Refuse a corrupt database at startup rather than limping on it.

        Live state sits in the -wal sidecar, so copying the directory while a bot
        is running yields an inconsistent snapshot. Left undetected it surfaces
        as a warning every cycle while the venues keep logging as though healthy,
        which can go unnoticed for hours.
        """
        try:
            state = self.db.execute("PRAGMA quick_check(1)").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            state = str(exc)
        if state != "ok":
            raise RuntimeError(
                f"{self.root / 'state.sqlite3'} is corrupt ({state}). A directory copied while a "
                "bot was running is the usual cause: SQLite keeps live state in the -wal sidecar "
                "and a mid-write copy is inconsistent. Stop every bot first, then either re-copy "
                "it or delete the data directory and let it rebuild — rebuilding restarts the "
                "paper accounts and their mark history, so keep a copy if that record matters.")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, dumps(value)))

    def event(self, kind, payload):
        with self.transaction() as db:
            db.execute("INSERT INTO events(ts,kind,payload) VALUES(?,?,?)", (time.time(), kind, dumps(payload)))

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def add_candles(self, market, symbol, rows):
        with self.transaction() as db:
            db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?)",
                           [(market, symbol, *r) for r in rows])

    def candles(self, market, symbol, limit=None):
        sql = "SELECT ts,close_ts,o,h,l,c,v,qv FROM candles WHERE market=? AND symbol=? ORDER BY ts"
        rows = self.rows(sql, (market, symbol))
        return rows[-limit:] if limit else rows

    def budget(self, maximum):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.transaction() as db:
            row = db.execute("SELECT n FROM requests WHERE day=?", (day,)).fetchone()
            n = row[0] if row else 0
            if n >= maximum:
                raise RuntimeError("Daily HTTP request budget exhausted; resume next UTC day.")
            db.execute("INSERT OR REPLACE INTO requests VALUES(?,?)", (day, n + 1))

    def close(self):
        self.db.close()
