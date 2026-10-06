"""HTTP: browser-like TLS, polite pauses, retries, raw-response cache, page-cache extraction."""
from __future__ import annotations

import hashlib
import json
import logging
import os
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
    def _pause(self, delay: tuple[float, float] | None = None):
        lo, hi = delay or self.delay
        wait = random.uniform(lo, hi) - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, headers: dict | None = None, *, delay: tuple[float, float] | None = None,
             timeout: float = 40, return_statuses: tuple[int, ...] = ()) -> tuple[int, str]:
        """GET with retries. Airbnb 403/429 count towards a block; other hosts' 429/5xx just back off longer.
        Permanent 4xx answers are raised at once (HTTPStatusError), statuses in return_statuses are returned."""
        airbnb = is_airbnb(url)
        attempt = 0
        while True:
            self._pause(delay)
            self._last_request = time.monotonic()
            self.network_requests += 1
            try:
                r = self.session.get(url, headers=headers or {}, timeout=timeout)
                status = r.status_code
            except Exception as e:  # network error
                status, r = None, None
                log.warning("network error %s: %s", url[:120], e)
            if status == 200 or status in return_statuses:
                if airbnb:
                    self._blocked_in_row = 0
                return status, r.text
            if airbnb and status in (403, 429):
                self._blocked_in_row += 1
                log.warning("Airbnb HTTP %s (blocked %d in a row), pausing %.0fs", status, self._blocked_in_row, self.block_pause)
                if self._blocked_in_row >= self.max_blocked:
                    raise BlockedError(f"Airbnb answered {status} {self._blocked_in_row} times in a row; stopping. "
                                       "Wait an hour and rerun the same command — already collected data is reused.")
                time.sleep(self.block_pause)
                continue
            if status is not None and 400 <= status < 500 and status not in (408, 425, 429):
                raise HTTPStatusError(status, url)
            attempt += 1
            if attempt > self.retries:
                raise NetworkError(f"GET failed after {self.retries} retries (last status {status}): {url[:200]}")
            # free community servers (Overpass, OSRM) are busy for minutes, not seconds
            backoff = (2 ** attempt if airbnb else 15 * 2 ** attempt) + random.random()
            if r is not None and (ra := r.headers.get("Retry-After", "")).isdigit():
                backoff = max(backoff, min(float(ra), 600))
            log.warning("HTTP %s from %s, retry %d in %.0fs", status, urllib.parse.urlsplit(url).netloc, attempt, backoff)
            time.sleep(backoff)

    def _cached_text(self, path: Path) -> str | None:
        try:
            return path.read_text() if path.exists() else None
        except (OSError, UnicodeDecodeError):
            path.unlink(missing_ok=True)
            return None

    def _cached_json(self, path: Path):
        text = self._cached_text(path)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:  # a half-written file from an interrupted run: fetch again
            log.warning("corrupt cache file %s, fetching again", path.name)
            path.unlink(missing_ok=True)
            return None

    # ---- public API --------------------------------------------------------------------------
    def page(self, url: str, cache_key: str, require: re.Pattern | None = None, *,
             delay: tuple[float, float] | None = None) -> str:
        """Text of a page, cached on disk under data/raw/. With `require`, a page that doesn't match it
        (a bot check, a redirect to another page) is returned but not cached, so a rerun fetches it again."""
        path = self._cache_path(cache_key, "html")
        text = self._cached_text(path)
        if text is not None and (require is None or require.search(text)):
            return text
        _, text = self._get(url, delay=delay)
        if require is None or require.search(text):
            atomic_write(path, text)
        else:
            log.warning("page without the expected content, not cached: %s", url[:120])
        if m := API_KEY_RE.search(text):
            self.api_key = m.group(1)
        return text

    def get_json(self, url: str, cache_key: str, delay: tuple[float, float] = (1.0, 1.5),
                 headers: dict | None = None, *, timeout: float = 60, return_statuses: tuple[int, ...] = (),
                 valid=None) -> dict:
        """Cached JSON GET for non-Airbnb free services (OSM Overpass, OSRM, FX): their own, shorter pause.
        `valid(data)` false → returned but not cached (e.g. an Overpass timeout reported inside a 200)."""
        path = self._cache_path(cache_key, "json")
        data = self._cached_json(path)
        if data is not None:
            return data
        _, text = self._get(url, headers={"User-Agent": USER_AGENT, **(headers or {})}, delay=delay,
                            timeout=timeout, return_statuses=return_statuses)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise NetworkError(f"not JSON from {urllib.parse.urlsplit(url).netloc}: {text[:120]!r}")
        if valid is None or valid(data):
            atomic_write(path, text)
        return data

    def graphql(self, operation: str, op_hash: str, variables: dict, cache_key: str) -> dict:
        path = self._cache_path(cache_key, "json")
        data = self._cached_json(path)
        if data is not None:
            return data
        q = lambda o: urllib.parse.quote(json.dumps(o, separators=(",", ":")))
        ext = {"persistedQuery": {"version": 1, "sha256Hash": op_hash}}
        url = (f"{BASE}/api/v3/{operation}/{op_hash}?operationName={operation}"
               f"&locale={self.locale}&currency={self.currency}&variables={q(variables)}&extensions={q(ext)}")
        _, text = self._get(url, headers={"x-airbnb-api-key": self.api_key, "content-type": "application/json"})
        try:
            data = json.loads(text)
        except json.JSONDecodeError:  # a bot-check page instead of JSON
            raise NetworkError(f"{operation}: Airbnb answered with a page instead of data; wait and rerun")
        if data.get("errors") and not data.get("data"):
            # Don't cache errors: a stale hash must be re-discovered, not remembered.
            raise GraphQLError(operation, data["errors"])
        atomic_write(path, text)
        return data

    def forget(self, cache_key: str, ext: str = "json") -> None:
        """Drop a cached answer that turned out to be bad (e.g. a truncated page)."""
        self._cache_path(cache_key, ext).unlink(missing_ok=True)

    def binary(self, url: str, dest: Path, delay: tuple[float, float] | None = None) -> bool:
        """Download an image (photos/map tiles for the page bundle). Skips existing files.
        CDN files get a shorter pause than airbnb.com pages."""
        if dest.exists() and dest.stat().st_size > 0:
            return True
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._pause(delay)
        self._last_request = time.monotonic()
        try:
            r = self.session.get(url, timeout=40, headers={"User-Agent": USER_AGENT})
        except Exception as e:
            log.warning("download failed %s: %s", url[:120], e)
            return False
        if r.status_code != 200:
            log.warning("download HTTP %s: %s", r.status_code, url[:120])
            return False
        if not is_image(r.content):
            log.warning("download is not an image, skipped: %s", url[:120])
            return False
        atomic_write(dest, r.content)
        return True


USER_AGENT = "airbnb-finder-skill/1.0 (trip planning; https://github.com/nstimofeev/airbnb-finder-skill)"


def is_airbnb(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host == "airbnb.com" or host.endswith(".airbnb.com")


def is_image(b: bytes) -> bool:
    """JPEG/PNG/WebP/GIF, or a web font (the page bundles those too): anything else is an error page."""
    return b[:4] in (b"wOF2", b"wOFF") or b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or b[:4] == b"RIFF" and b[8:12] == b"WEBP" \
        or b[:6] in (b"GIF87a", b"GIF89a")


def atomic_write(path: Path, data: str | bytes) -> None:
    """Write via a temp file + rename, so an interrupted run never leaves half a file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class HTTPStatusError(RuntimeError):
    """A permanent 4xx answer (404 listing gone, 400 bad request): retrying won't help."""
    def __init__(self, status: int, url: str):
        self.status, self.url = status, url
        super().__init__(f"HTTP {status}: {url[:200]}")


class NetworkError(RuntimeError):
    """Temporary trouble that outlasted the retries: rerunning later usually helps."""


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
