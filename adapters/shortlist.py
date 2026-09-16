"""Merge candidate addresses from several providers into one deduplicated list.

These providers have already indexed the chain, so they answer the question this
project cannot afford to: which few hundred addresses out of millions are worth
reconstructing. What they return is a coarse screen, not a ranking — their PnL
figures use their own cost conventions and none of them account for transferred
inventory the way `twobots.wallets` does. Nothing here is scored; the shortlist
only decides what gets examined.

Sources are declared in config rather than coded per provider, because a REST
endpoint that returns addresses is the same shape whoever serves it, and these
APIs change more often than this file should.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time

from twobots.wallets import valid_address

MAX_PER_SOURCE = 1000


def resolve(value):
    """`env:NAME` becomes that environment variable; anything else is literal."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.getenv(value[4:], "")
    return value


def headers_for(source):
    """-> (headers, missing_env_names). A source without its key is skipped, not failed."""
    headers, missing = {}, []
    for name, raw in (source.get("headers") or {}).items():
        value = resolve(raw)
        if isinstance(raw, str) and raw.startswith("env:") and not value:
            missing.append(raw[4:])
            continue
        headers[name] = str(value)
    return headers, missing


def extract(payload, path):
    """Pull every value at a dotted path. `[]` walks into a list.

    `result.rows[].trader` covers Dune, `data.items[].address` covers the usual
    REST shape; one expression instead of a parser per provider.
    """
    values = [payload]
    for part in path.split("."):
        step = []
        listed = part.endswith("[]")
        key = part[:-2] if listed else part
        for value in values:
            if key:
                if not isinstance(value, dict) or key not in value:
                    continue
                value = value[key]
            if listed:
                step.extend(value if isinstance(value, list) else [])
            else:
                step.append(value)
        values = step
    return values


def from_file(source):
    """A CSV column, a JSON list, or one address per line."""
    with open(source["path"], encoding="utf-8-sig") as handle:
        text = handle.read()
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        payload = json.loads(stripped)
        return extract(payload, source["address_path"]) if source.get("address_path") \
            else [str(x) for x in payload]
    column = source.get("column")
    if column:
        return [row[column] for row in csv.DictReader(io.StringIO(text)) if row.get(column)]
    return [line.strip() for line in text.splitlines() if line.strip()]


def from_http(source, http):
    headers, missing = headers_for(source)
    if missing:
        raise PermissionError("missing " + ", ".join(missing))
    raw = http.request(source["url"], source.get("params"), headers,
                       json.dumps(source["body"]).encode() if source.get("body") else None,
                       max_bytes=source.get("max_mb", 16) * 1024 * 1024)
    return extract(json.loads(raw), source["address_path"])


def safe_detail(exc):
    """A status code separates a bad key from a wrong path from a real block.

    Only messages this project builds itself are passed through; a provider's own
    text can echo the query string, and that carries the key.
    """
    text = str(exc)
    if re.fullmatch(r"HTTP \d{3} from [\w.-]+;.*", text) or text.startswith("Network timeout"):
        return text
    return type(exc).__name__


def suggest_paths(payload, prefix="", found=None, depth=0):
    """Dotted paths in a response whose values look like Solana addresses.

    Every provider puts them somewhere different, and reading their docs to find
    out is the slowest part of adding a source. Recognising the addresses is both
    faster and harder to get wrong than reading a schema.
    """
    found = {} if found is None else found
    if depth > 8:
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            suggest_paths(value, f"{prefix}.{key}" if prefix else key, found, depth + 1)
    elif isinstance(payload, list):
        for item in payload[:50]:
            suggest_paths(item, prefix + "[]", found, depth + 1)
    elif valid_address(payload):
        found[prefix] = found.get(prefix, 0) + 1
    return found


# A mint, a pool and a wallet are all 32-byte base58, so shape alone cannot tell
# them apart. These segments name the things that are not wallets.
NOT_A_WALLET = ("token", "mint", "pool", "base", "quote", "market", "pair",
                "program", "vault", "lp", "from", "to")


def looks_like_a_wallet(path):
    return not any(any(word in segment for word in NOT_A_WALLET)
                   for segment in path.replace("[]", "").split("."))


def probe(url, headers, http, params=None):
    """-> (wallet-ish paths, other address paths, top-level keys).

    Feeding a token mint into a wallet ranker produces confident nonsense, so
    paths naming something other than a wallet are separated out rather than
    ranked alongside.
    """
    resolved, missing = headers_for({"headers": headers})
    if missing:
        raise PermissionError("missing " + ", ".join(missing))
    payload = json.loads(http.request(url, params, resolved, max_bytes=16 * 1024 * 1024))
    ranked = sorted(suggest_paths(payload).items(), key=lambda kv: -kv[1])
    wallets = {p: n for p, n in ranked if looks_like_a_wallet(p)}
    others = {p: n for p, n in ranked if not looks_like_a_wallet(p)}
    return wallets, others, sorted(payload) if isinstance(payload, dict) else ["<list>"]


def collect(sources, http, now=None):
    """-> (addresses -> the sources that named it, per-source report).

    One provider failing never loses the others: each is reported on its own.
    """
    now = time.time() if now is None else now
    merged, report = {}, []
    for source in sources:
        name = source.get("name") or source.get("kind", "source")
        try:
            found = from_file(source) if source.get("kind") == "file" else from_http(source, http)
        except PermissionError as exc:
            report.append({"source": name, "skipped": str(exc)})
            continue
        except Exception as exc:
            report.append({"source": name, "error": safe_detail(exc)})
            continue
        kept, rejected = [], 0
        for value in found[:MAX_PER_SOURCE]:
            if valid_address(value):
                kept.append(value)
            else:
                rejected += 1
        for address in kept:
            entry = merged.setdefault(address, {"sources": [], "first_seen": now})
            if name not in entry["sources"]:
                entry["sources"].append(name)
        report.append({"source": name, "returned": len(found), "valid": len(kept),
                       "invalid": rejected})
    return merged, report


def rank_by_agreement(merged, limit):
    """Addresses several providers independently surfaced go first.

    Agreement is not evidence of skill — the providers share data vendors and
    conventions — but it is a cheap tiebreak for which to reconstruct first.
    """
    order = sorted(merged, key=lambda a: (-len(merged[a]["sources"]), merged[a]["first_seen"], a))
    return order[:limit]
