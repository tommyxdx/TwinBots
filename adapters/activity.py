"""Near-real-time fills for the wallets the copy trader follows.

    python -m adapters.activity poll --out wallet_activity --data-dir data --watch
    python -m adapters.activity webhook --out wallet_activity --port 8788

Poll mode asks the RPC for each leader's recent transactions on an interval.
Webhook mode receives Helius pushes instead, which costs one credit per event
rather than one call per leader per interval; it needs a publicly reachable
address, so put a tunnel or a small proxy in front of it.

Leaders are read from the bot's own store, so this never ranks anything itself.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

from .helius import Helius
from .prices import SolPrice, quote_pricer
from .reconstruct import classify, normalize, quote_usd

MAX_FILLS = 500
MAX_BODY = 8 * 1024 * 1024


def leaders_from_store(data_dir):
    """Addresses the copy trader is currently following, written by CopyTrader.step."""
    path = Path(data_dir) / "state.sqlite3"
    if not path.exists():
        raise ValueError(f"No bot state at {path}; run the scanner and copy trader first")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        row = db.execute("SELECT v FROM kv WHERE k='copy:leaders'").fetchone()
    finally:
        db.close()
    if not row:
        return []
    return json.loads(row[0]).get("addresses", [])


def fills_from_entries(entries, address, price_usd):
    """Leader fills. Unlike the ledger this needs no cost basis, only direction and size."""
    fills = []
    for row in sorted((normalize(e, address) for e in entries),
                      key=lambda r: (r["ts"], r["slot"], r["order"])):
        side, mint, quantity, notional = classify(row, quote_usd(row, price_usd))
        if side not in ("buy", "sell"):
            continue
        fills.append({"id": row["signature"] + ":0", "ts": row["ts"], "side": side,
                      "token": mint, "notional_usd": str(notional)})
    return fills[-MAX_FILLS:]


def write_feed(out_dir, address, fills, asof=None, source="helius-poll/1.2.0"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "network": "solana", "address": address,
               "asof": int(asof if asof is not None else time.time()),
               "source": source, "fills": fills}
    path = out / (address + ".json")
    temp = path.with_suffix(".part")
    # Atomic replace: the bot must never read a half-written feed.
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return path


def poll_once(addresses, helius, sol_price, out_dir, lookback_s):
    now = time.time()
    sol_price.load(now - lookback_s - 3600, now)
    price_usd = quote_pricer(sol_price)
    written = {}
    for address in addresses:
        entries = list(helius.transactions(address, since_ts=int(now - lookback_s), max_pages=5))
        fills = fills_from_entries(entries, address, price_usd)
        write_feed(out_dir, address, fills, now)
        written[address] = len(fills)
    return written


def webhook_handler(out_dir, sol_price, secret):
    """Accepts Helius enhanced/raw webhook pushes and appends to each leader's feed."""
    price_usd = quote_pricer(sol_price)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log URLs or bodies; they carry addresses and the shared secret.

        def do_POST(self):
            code, body = 200, b'{"ok":true}'
            try:
                if secret and self.headers.get("Authorization") != secret:
                    raise PermissionError("bad secret")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ValueError("payload too large")
                entries = json.loads(self.rfile.read(length))
                if not isinstance(entries, list):
                    entries = [entries]
                self.ingest(entries)
            except PermissionError:
                code, body = 401, b'{"error":"unauthorized"}'
            except Exception:
                code, body = 400, b'{"error":"rejected"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def ingest(self, entries):
            owners = set()
            for entry in entries:
                for key in ("preTokenBalances", "postTokenBalances"):
                    for balance in (entry.get("meta") or {}).get(key) or []:
                        if balance.get("owner"):
                            owners.add(balance["owner"])
            now = time.time()
            sol_price.load(now - 2 * 86400, now)
            for address in owners:
                new = fills_from_entries(entries, address, price_usd)
                if not new:
                    continue
                path = Path(out_dir) / (address + ".json")
                existing = []
                if path.exists():
                    try:
                        existing = json.loads(path.read_text(encoding="utf-8")).get("fills", [])
                    except Exception:
                        existing = []
                merged = {f["id"]: f for f in existing}
                merged.update({f["id"]: f for f in new})
                fills = sorted(merged.values(), key=lambda f: (f["ts"], f["id"]))[-MAX_FILLS:]
                write_feed(out_dir, address, fills, now, "helius-webhook/1.2.0")

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("poll", help="Ask the RPC for each leader's recent transactions")
    p.add_argument("--out", default="wallet_activity")
    p.add_argument("--cache", default="data/downloads")
    p.add_argument("--data-dir", default="data", help="Bot data_dir, to read the leader list")
    p.add_argument("--address", action="append", default=[],
                   help="Poll this address instead of the bot's leader list; repeatable")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--lookback", type=int, default=900)
    p.add_argument("--watch", action="store_true")
    w = sub.add_parser("webhook", help="Receive Helius pushes on loopback")
    w.add_argument("--out", default="wallet_activity")
    w.add_argument("--cache", default="data/downloads")
    w.add_argument("--port", type=int, default=8788)
    w.add_argument("--listen", default="127.0.0.1")
    w.add_argument("--secret-env", default="WALLET_WEBHOOK_SECRET")
    args = parser.parse_args(argv)

    if args.mode == "webhook":
        import os
        server = ThreadingHTTPServer(
            (args.listen, args.port),
            webhook_handler(args.out, SolPrice(args.cache), os.getenv(args.secret_env, "")))
        print(json.dumps({"listening": f"{args.listen}:{args.port}", "out": args.out,
                          "note": "Put HTTPS and the shared secret in front of this."}))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    helius, sol_price = Helius(), SolPrice(args.cache)
    while True:
        addresses = args.address or leaders_from_store(args.data_dir)
        if not addresses:
            print(json.dumps({"leaders": 0, "note": "No eligible leader yet; run scan first."}))
        else:
            try:
                written = poll_once(addresses, helius, sol_price, args.out, args.lookback)
                print(json.dumps({"at": int(time.time()), "fills_by_leader": written,
                                  "rpc_calls": helius.calls}))
            except Exception as exc:
                print(json.dumps({"at": int(time.time()), "error": str(exc)}))
        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
