from __future__ import annotations
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger(__name__)


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect is a new destination and an unbudgeted request. Never
        # forward API keys, signed query strings or notification tokens.
        raise ValueError("HTTP redirects are disabled; configure the final trusted endpoint")


def safe_urlopen(req, timeout):
    return urllib.request.build_opener(RejectRedirects()).open(req, timeout=timeout)


def auth_headers(cfg, prefix=""):
    env = cfg.get(prefix + "api_key_env", "")
    value = os.getenv(env, "")
    return {cfg.get(prefix + "header", "Authorization"):
            cfg.get(prefix + "header_prefix", "") + value} if value else {}


class HTTP:
    """Bounded retry/cache caller. Never logs secret URLs, headers, or response bodies."""
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg["http"], store
        self.lock = threading.Lock()
        self.last = {}

    def request(self, url, params=None, headers=None, body=None, max_bytes=None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
            raise ValueError("Data endpoints must use HTTPS (except localhost test adapters).")
        cap = max_bytes or int(self.cfg["max_download_mb"] * 1024 * 1024)
        retries = self.cfg["retries"] if body is None else 0  # POST delivery may be ambiguous.
        for attempt in range(retries + 1):
            self.store.budget(self.cfg["max_requests_per_day"])
            with self.lock:
                delay = self.cfg.get("host_intervals", {}).get(parsed.hostname, self.cfg["min_interval_s"])
                time.sleep(max(0, self.last.get(parsed.hostname, 0) + delay - time.monotonic()))
                self.last[parsed.hostname] = time.monotonic()
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "TwinCryptoBots/1.1.1", **(headers or {})})
            start = time.monotonic()
            try:
                with safe_urlopen(req, timeout=self.cfg["timeout_s"]) as r:
                    raw = r.read(cap + 1)
                if len(raw) > cap:
                    raise ValueError("Download exceeds configured size limit")
                self.store.set("http:last_latency_ms:" + parsed.hostname, (time.monotonic() - start) * 1000)
                return raw
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    raise RuntimeError(f"HTTP {exc.code} from {parsed.hostname}; inspect endpoint/access locally") from None
                retry = exc.headers.get("Retry-After", "")
                time.sleep(min(30, float(retry) if retry.isdigit() else 2 ** attempt))
            except (OSError, TimeoutError):
                if attempt == retries:
                    raise RuntimeError(f"Network timeout/error from {parsed.hostname}") from None
                time.sleep(2 ** attempt)
        raise RuntimeError("Request failed")

    def json(self, url, params=None, headers=None, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        h = {**(headers or {})}
        if body is not None:
            h["Content-Type"] = "application/json"
        return json.loads(self.request(url, params, h, body))
