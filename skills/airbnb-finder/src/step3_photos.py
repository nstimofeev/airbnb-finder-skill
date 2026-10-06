"""Step 3: photo URLs (with host captions) for finalists → data/05_photos.json. Links only, no downloads."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .fetch import atomic_write, Fetcher, dig, extract_deferred_state, niobe_entry
from .step1_search import trip_key
from .step2_details import room_url

log = logging.getLogger(__name__)


def caption_text(c) -> str | None:
    """Captions come as a string or as {user: {localizedStringWithTranslationPreference: ...}}."""
    if isinstance(c, dict):
        u = c.get("user") or c
        c = u.get("localizedStringWithTranslationPreference") or u.get("localizedString") or u.get("source")
    return c.strip() if isinstance(c, str) and c.strip() else None


def parse_photos(state: dict | None) -> list[dict]:
    resp = niobe_entry(state, "StaysPdpSections") or {}
    photos, seen = [], set()
    # Photo tour grouped by room: caption + room name
    for stop in dig(resp, "data", "node", "pdpPresentation", "mediaTour", "stops", default=[]):
        for it in stop.get("items") or []:
            img = it.get("image") or {}
            url = img.get("uri") or img.get("baseUrl")
            if url and url not in seen:
                seen.add(url)
                photos.append({"url": url, "caption": caption_text(img.get("caption")), "room": stop.get("name")})
    # Fallback / extras: the photo-tour modal section
    for s in dig(resp, "data", "presentation", "stayProductDetailPage", "sections", "sections", default=[]):
        for m in dig(s, "section", "mediaItems", default=[]):
            url = m.get("baseUrl")
            if url and url not in seen:
                seen.add(url)
                cap = dig(m, "imageMetadata", "localizedCaption") or dig(m, "imageMetadata", "caption")
                photos.append({"url": url, "caption": caption_text(cap), "room": None})
    return photos


def run(cfg: dict, data_dir: Path, f: Fetcher, force: bool = False) -> Path:
    out = data_dir / "05_photos.json"
    finalists = json.loads((data_dir / "04_finalists.json").read_text())["finalists"]
    have = json.loads(out.read_text()) if out.exists() and not force else {}
    for fin in finalists:
        lid = str(fin["id"])
        if lid in have:
            continue
        html = f.page(room_url(lid, cfg), f"room_{lid}_{trip_key(cfg)}")  # cached since step 2
        photos = parse_photos(extract_deferred_state(html))
        if not photos:
            log.warning("%s: no photos found (page structure changed?)", lid)
        have[lid] = photos
        log.info("step3 %s: %d photos", lid, len(photos))
    atomic_write(out, json.dumps(have, ensure_ascii=False, indent=1))
    return out
