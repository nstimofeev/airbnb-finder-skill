"""Hashes of Airbnb's persisted GraphQL queries.

They are plain constants inside the site's JS bundles (not a signature), but they change when Airbnb
redeploys. Known values are tried first; on a "persisted query not found" error we re-read them from
the bundles of a listing page and remember them in data/raw/ophashes.json.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .fetch import Fetcher

log = logging.getLogger(__name__)

KNOWN = {
    "StaysPdpReviewsQuery": "cfdc3ffbe997a618795fc5a8f9a9b484054ce9be68c8788cd2ffda999934c5ae",
    "StaysPdpSections": "e684d30a625b62ffc44534b933a39e82ca704d9176de1e27eaffa02dd68df990",
    "StaysPdpPoliciesQuery": "e509b511efafbdb5fc80163cd9a829a60cef906027994e0c0c4e477e53a6888d",
}
# Lazy chunks to look into when the hash is not in the page's initial bundles.
CHUNK_HINTS = {
    "StaysPdpReviewsQuery": ("gp-stays-pdp-sections/migrated-sections/ReviewsSection/",
                             "gp-stays-pdp-sections/subpages/Reviews/ReviewsModalContent"),
    "StaysPdpSections": ("PdpPlatformRoute",),
    "StaysPdpPoliciesQuery": ("gp-stays-pdp-sections/sections/Policies/PoliciesSection",),
}
STATIC = "https://a0.muscache.com/airbnb/static/packages/web/"
SCRIPT_RE = re.compile(r'https://a0\.muscache\.com/[^"\']+\.js')


def op_re(name: str) -> re.Pattern:
    return re.compile(r"name:'" + re.escape(name) + r"',type:'query',operationId:'([0-9a-f]{64})'")


class OpHashes:
    def __init__(self, fetcher: Fetcher, store: Path):
        self.f = fetcher
        self.store = Path(store)
        self.hashes = dict(KNOWN)
        if self.store.exists():
            self.hashes.update(json.loads(self.store.read_text()))

    def get(self, name: str) -> str:
        return self.hashes[name]

    def rediscover(self, name: str, page_html: str) -> str:
        found = find_in_bundles(self.f, name, page_html)
        if not found:
            raise RuntimeError(f"Could not find the current hash of {name} in airbnb.com bundles — "
                               "the site structure changed, the parser needs an update.")
        log.info("new hash for %s: %s", name, found)
        self.hashes[name] = found
        self.store.write_text(json.dumps(self.hashes, indent=1))
        return found


def find_in_bundles(f: Fetcher, name: str, page_html: str) -> str | None:
    rx = op_re(name)
    urls = sorted(set(SCRIPT_RE.findall(page_html)))
    chunk_map = ""
    for u in urls:
        js = f.page(u, "js_" + u.rsplit("/", 1)[-1])
        if m := rx.search(js):
            return m.group(1)
        if "asyncRequire" in u:
            chunk_map = js
    for hint in CHUNK_HINTS.get(name, ()):
        for path in sorted(set(re.findall(r'"([^"]*' + re.escape(hint) + r'[^"]*\.js)"', chunk_map))):
            js = f.page(STATIC + path, "js_" + path.rsplit("/", 1)[-1])
            if m := rx.search(js):
                return m.group(1)
    return None
