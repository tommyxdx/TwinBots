"""Paginating read-only client for Helius getTransactionsForAddress.

Only history is read. No key with signing authority is involved, and nothing
here can submit a transaction.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

ENDPOINT = "https://mainnet.helius-rpc.com"
# Documented maximum is 1000 full transactions; 500 keeps a jsonParsed page well
# inside the response cap while still cutting round trips on busy wallets.
PAGE = 500
RETRY_CODES = (429, 500, 502, 503, 504)
# The chain gains transaction versions over time and the RPC refuses any it
# believes the client cannot read. Reconstruction here is done from balance
# deltas and never parses instructions, so every version is equally readable:
# the ceiling exists only to satisfy the endpoint.
UNSUPPORTED_VERSION = re.compile(r"[Tt]ransaction version \((\d+)\) is not supported")


def unsupported_version(message):
    """The version an RPC refusal is asking for, or None if that is not the complaint."""
    found = UNSUPPORTED_VERSION.search(message or "")
    return int(found.group(1)) if found else None


class TooMuchHistory(RuntimeError):
    """The wallet has more history than the configured page budget allows."""


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirects are disabled; the API key must not be forwarded")


class Helius:
    # A class attribute as well as an instance one: the ceiling has a meaningful
    # default before __init__ runs, so a partially constructed client still
    # builds a valid request rather than failing on a missing attribute.
    max_tx_version = 0

    def __init__(self, api_key=None, endpoint=ENDPOINT, timeout=60, min_interval_s=0.15,
                 max_retries=3, max_bytes=64 * 1024 * 1024):
        self.api_key = api_key or os.getenv("HELIUS_API_KEY", "")
        if not self.api_key:
            raise ValueError("Set HELIUS_API_KEY; the adapter reads history only")
        self.endpoint, self.timeout = endpoint, timeout
        self.min_interval_s, self.max_retries, self.max_bytes = min_interval_s, max_retries, max_bytes
        self.opener = urllib.request.build_opener(RejectRedirects())
        self.last, self.calls = 0.0, 0
        self.max_tx_version = 0

    def rpc(self, method, params):
        encode = lambda: json.dumps({"jsonrpc": "2.0", "id": "adapter", "method": method,
                                     "params": params}).encode()
        body = encode()
        # The key travels in the query string as Helius requires, so it must never
        # reach a log or an exception message.
        url = f"{self.endpoint}/?api-key={self.api_key}"
        for attempt in range(self.max_retries + 1):
            wait = self.last + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json",
                                         "User-Agent": "TwinCryptoBots-Adapter/1.2.0"})
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise ValueError("RPC response exceeds size limit")
                self.calls += 1
                payload = json.loads(raw)
                if "error" in payload:
                    message = payload["error"].get("message", "unknown")
                    # Raise the declared ceiling to whatever the endpoint asked
                    # for and try again. Failing instead loses the wallet and
                    # waits for someone to notice that a constant needs bumping
                    # -- which is one build failure buried in an hourly log.
                    wanted = unsupported_version(message)
                    if wanted is not None and wanted > self.max_tx_version and attempt < self.max_retries:
                        self.max_tx_version = wanted
                        for item in params:
                            if isinstance(item, dict) and "maxSupportedTransactionVersion" in item:
                                item["maxSupportedTransactionVersion"] = wanted
                        body = encode()
                        continue
                    raise RuntimeError(f"{method} failed: {message}")
                return payload["result"]
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_CODES or attempt == self.max_retries:
                    raise RuntimeError(f"HTTP {exc.code} from the RPC endpoint") from None
                retry = exc.headers.get("Retry-After", "")
                time.sleep(min(30, float(retry) if retry.isdigit() else 2 ** attempt))
            except (OSError, TimeoutError):
                if attempt == self.max_retries:
                    raise RuntimeError("Network timeout reaching the RPC endpoint") from None
                time.sleep(2 ** attempt)
        raise RuntimeError("RPC request failed")

    def transactions(self, address, since_ts=None, until_ts=None, limit=PAGE, max_pages=400,
                     sort_order="asc", partial_ok=False):
        """Full transactions touching `address`, including its token accounts.

        Ascending by default so a partial run still yields a contiguous prefix of
        history rather than a hole in the middle of it.

        `partial_ok` distinguishes a caller that wants a sample from one that
        needs the whole history: running out of pages is a normal stop for the
        first and a failure for the second.
        """
        options = {"transactionDetails": "full", "encoding": "jsonParsed",
                   "commitment": "finalized", "maxSupportedTransactionVersion": self.max_tx_version,
                   "sortOrder": sort_order, "limit": limit,
                   "filters": {"tokenAccounts": "balanceChanged", "status": "any"}}
        block_time = {}
        if since_ts is not None:
            block_time["gte"] = int(since_ts)
        if until_ts is not None:
            block_time["lte"] = int(until_ts)
        if block_time:
            options["filters"]["blockTime"] = block_time
        token, pages = None, 0
        while pages < max_pages:
            if token:
                options["paginationToken"] = token
            result = self.rpc("getTransactionsForAddress", [address, options])
            for entry in result.get("data") or []:
                if entry.get("blockTime"):
                    yield entry
            token, pages = result.get("paginationToken"), pages + 1
            if not token:
                return
        if not partial_ok:
            raise TooMuchHistory(f"History exceeded {max_pages} pages of {limit}")

    def history_size(self, address, probe_pages=2, limit=PAGE):
        """Signatures only, to size a wallet before paying for its full history.

        Walking a market maker's whole ledger and then abandoning it at the page
        cap spends the entire budget for nothing, so the cheap shape of the
        history is checked first.
        """
        options = {"transactionDetails": "signatures", "commitment": "finalized",
                   "maxSupportedTransactionVersion": self.max_tx_version,
                   "sortOrder": "desc", "limit": limit,
                   "filters": {"tokenAccounts": "balanceChanged", "status": "any"}}
        seen, oldest, token = 0, None, None
        for _ in range(probe_pages):
            if token:
                options["paginationToken"] = token
            result = self.rpc("getTransactionsForAddress", [address, options])
            rows = result.get("data") or []
            seen += len(rows)
            for row in rows:
                if row.get("blockTime"):
                    oldest = min(oldest or row["blockTime"], row["blockTime"])
            token = result.get("paginationToken")
            if not token:
                return {"transactions": seen, "complete": True, "oldest": oldest}
        return {"transactions": seen, "complete": False, "oldest": oldest}
