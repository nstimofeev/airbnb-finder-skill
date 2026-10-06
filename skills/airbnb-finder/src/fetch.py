"""HTTP: browser-like TLS, polite pauses, retries, raw-response cache, page-cache extraction."""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

log = logging.getLogger(__name__)

BASE = "https://www.airbnb.com"
# Public web key that airbnb.com embeds in every page (api_config.key). Re-read from a page when possible.
DEFAULT_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
DEFERRED_RE = re.compile(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', re.S)
API_KEY_RE = re.compile(r'"api_config":\{"key":"([a-z0-9]+)"')


class BlockedError(RuntimeError):
    """Airbnb keeps refusing (403/429) — stop instead of hammering."""


class Fetcher:
    def __init__(self, raw_dir: Path, net: dict | None = None):
        net = net or {}
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.delay = tuple(net.get("delay_s", (2, 5)))
        self.retries = int(net.get("retries", 4))
        self.block_pause = float(net.get("block_pause_s", 90))
        self.max_blocked = int(net.get("max_blocked_in_row", 3))
        self.locale = net.get("locale", "en")
        self.currency = net.get("currency", "USD")
        self.session = requests.Session(impersonate="chrome")
        self.api_key = DEFAULT_API_KEY
        self._blocked_in_row = 0
        self._last_request = 0.0
        self.network_requests = 0
        self.session_resets = 0

    def reset_session(self) -> None:
        """Fresh cookies. Airbnb's bot protection silently truncates answers for a session after a series
        of requests (reviews pages come back with 2 items); a new session gets full answers again."""
        self.session = requests.Session(impersonate="chrome")
        self.session_resets += 1
        log.info("HTTP session reset (fresh cookies), #%d", self.session_resets)

    # ---- cache -------------------------------------------------------------------------------
    def _cache_path(self, key: str, ext: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[:80]  # short: Windows paths are capped at 260
        digest = hashlib.sha1(key.encode()).hexdigest()[:10]
        return self.raw_dir / f"{safe}.{digest}.{ext}"

    # ---- network -----------------------------------------------------------------------------
    def _pause(self):
        wait = random.uniform(*self.delay) - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, headers: dict | None = None) -> str:
        attempt = 0
        while True:
            self._pause()
            self._last_request = time.monotonic()
            self.network_requests += 1
            try:
                r = self.session.get(url, headers=headers or {}, timeout=40)
                status = r.status_code
            except Exception as e:  # network error
                status, r = None, None
                log.warning("network error %s: %s", url[:120], e)
            if status == 200:
                self._blocked_in_row = 0
                return r.text
            if status in (403, 429):
                self._blocked_in_row += 1
                log.warning("HTTP %s (blocked %d in a row), pausing %.0fs", status, self._blocked_in_row, self.block_pause)
                if self._blocked_in_row >= self.max_blocked:
                    raise BlockedError(f"Airbnb answered {status} {self._blocked_in_row} times in a row; stopping. "
                                       "Wait an hour and rerun — already collected data is cached.")
                time.sleep(self.block_pause)
                continue
            attempt += 1
            if attempt > self.retries:
                raise RuntimeError(f"GET failed after {self.retries} retries (last status {status}): {url[:200]}")
            backoff = 2 ** attempt + random.random()
            log.warning("HTTP %s, retry %d in %.1fs", status, attempt, backoff)
            time.sleep(backoff)

    # ---- public API --------------------------------------------------------------------------
    def page(self, url: str, cache_key: str) -> str:
        """HTML of a page, cached on disk under data/raw/."""
        path = self._cache_path(cache_key, "html")
        if path.exists():
            return path.read_text()
        text = self._get(url)
        path.write_text(text)
        if m := API_KEY_RE.search(text):
            self.api_key = m.group(1)
        return text

    def get_json(self, url: str, cache_key: str, delay: tuple[float, float] = (1.0, 1.5),
                 headers: dict | None = None) -> dict:
        """Cached JSON GET for non-Airbnb free services (OSM Overpass, OSRM): their own, shorter pause."""
        path = self._cache_path(cache_key, "json")
        if path.exists():
            return json.loads(path.read_text())
        saved = self.delay
        self.delay = delay
        try:
            text = self._get(url, headers={"User-Agent": "airbnb-finder-skill/1.0 (trip planning)", **(headers or {})})
        finally:
            self.delay = saved
        data = json.loads(text)
        path.write_text(text)
        return data

    def graphql(self, operation: str, op_hash: str, variables: dict, cache_key: str) -> dict:
        path = self._cache_path(cache_key, "json")
        if path.exists():
            return json.loads(path.read_text())
        q = lambda o: urllib.parse.quote(json.dumps(o, separators=(",", ":")))
        ext = {"persistedQuery": {"version": 1, "sha256Hash": op_hash}}
        url = (f"{BASE}/api/v3/{operation}/{op_hash}?operationName={operation}"
               f"&locale={self.locale}&currency={self.currency}&variables={q(variables)}&extensions={q(ext)}")
        text = self._get(url, headers={"x-airbnb-api-key": self.api_key, "content-type": "application/json"})
        data = json.loads(text)
        if data.get("errors") and not data.get("data"):
            # Don't cache errors: a stale hash must be re-discovered, not remembered.
            raise GraphQLError(operation, data["errors"])
        path.write_text(text)
        return data

    def forget(self, cache_key: str, ext: str = "json") -> None:
        """Drop a cached answer that turned out to be bad (e.g. a truncated page)."""
        self._cache_path(cache_key, ext).unlink(missing_ok=True)

    def binary(self, url: str, dest: Path, delay: tuple[float, float] | None = None) -> bool:
        """Download a file (photos/map tiles for the shareable bundle). Skips existing files.
        CDN files get a shorter pause than airbnb.com pages."""
        if dest.exists() and dest.stat().st_size > 0:
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        lo, hi = delay or self.delay
        wait = random.uniform(lo, hi) - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        try:
            r = self.session.get(url, timeout=40, headers={"User-Agent": "airbnb-finder-skill/1.0 (trip planning)"})
        except Exception as e:
            log.warning("download failed %s: %s", url[:120], e)
            return False
        if r.status_code != 200:
            log.warning("download HTTP %s: %s", r.status_code, url[:120])
            return False
        dest.write_bytes(r.content)
        return True


class GraphQLError(RuntimeError):
    def __init__(self, operation: str, errors):
        self.operation, self.errors = operation, errors
        super().__init__(f"{operation}: {json.dumps(errors)[:400]}")


def extract_deferred_state(html: str) -> dict | None:
    """The JSON cache Airbnb embeds in <script id="data-deferred-state-0">."""
    m = DEFERRED_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def niobe_entry(state: dict | None, prefix: str) -> dict | None:
    """niobeClientData is a list of [query_key, response]; return the response whose key starts with prefix."""
    for entry in (state or {}).get("niobeClientData", []) or []:
        if isinstance(entry, list) and len(entry) == 2 and str(entry[0]).startswith(prefix):
            return entry[1]
    return None


def dig(obj, *path, default=None):
    """Safe nested get: dig(d, "a", 0, "b")."""
    for p in path:
        try:
            obj = obj[p]
        except (KeyError, IndexError, TypeError):
            return default
        if obj is None:
            return default
    return obj
