"""Step 1: crawl the search zone, dedupe, mechanical filter → data/01_search.json."""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from datetime import date
from pathlib import Path

from .fetch import DEFERRED_RE, atomic_write, BASE, Fetcher, dig, extract_deferred_state, niobe_entry
from .geo import BBox, FUZZ_M, bbox_around_anchors, check_anchors

log = logging.getLogger(__name__)

SEARCH_CAP = 270  # Airbnb never pages past ~15 x 18 results per search
MAX_DEPTH = 6
ROOM_TYPES = {  # config value -> Airbnb filter value
    "entire_home": "Entire home/apt",
    "private_room": "Private room",
    "hotel_room": "Hotel room",
    "shared_room": "Shared room",
}
BADGES = {"TOP_X_GUEST_FAVORITE": "guest_favorite", "GUEST_FAVORITE": "guest_favorite", "SUPERHOST": "superhost"}
MONEY_RE = re.compile(r"[\d.,]+")


# ---- parsing (pure) -----------------------------------------------------------------------------
def parse_money(s: str | None) -> float | None:
    if not s:
        return None
    m = MONEY_RE.search(s.replace(" ", " "))
    if not m:
        return None
    num = m.group(0)
    # "1,264.38" (en) or "1.264,38" (other locales)
    if "," in num and "." in num:
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", "") if re.search(r",\d{3}$", num) else num.replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def decode_listing_id(gid: str | None) -> str | None:
    """'RGVtYW5kU3RheUxpc3Rpbmc6MjM0MTEzOTM=' -> '23411393'."""
    if not gid:
        return None
    try:
        raw = base64.b64decode(gid + "=" * (-len(gid) % 4)).decode()
        lid = raw.rsplit(":", 1)[-1]
    except Exception:
        lid = gid
    return lid if lid.isdigit() else None  # ids become file names: digits only


def search_results(state: dict | None) -> dict | None:
    resp = niobe_entry(state, "StaysSearch")
    return dig(resp, "data", "presentation", "staysSearch", "results")


def total_hint(results: dict) -> int | None:
    """'Over 1,000 homes' -> 1001, '214 homes' -> 214; None if absent."""
    sections = dig(results, "sectionConfiguration", "pageTitleSections", "sections", default=[])
    for s in sections:
        title = dig(s, "sectionData", "structuredTitle")
        if not title:
            continue
        n = parse_money(title)
        if n is None:
            continue
        return int(n) + (1 if title.lower().startswith("over") else 0)
    return None


def _count(lines: list[str], pattern: str) -> float | None:
    """First number in a line matching pattern: '2 bedrooms' -> 2, 'Half-bath' -> 0.5, 'Studio' -> 0."""
    for line in lines:
        low = line.lower()
        if re.search(pattern, low):
            if m := re.search(r"(\d+(?:\.\d+)?)", low):
                return float(m.group(1))
            if "half" in low:
                return 0.5
            if "studio" in low:
                return 0
    return None


def parse_result(x: dict, nights: int, room_type: str | None, missing: set) -> dict | None:
    lid = decode_listing_id(dig(x, "demandStayListing", "id"))
    if not lid:
        missing.add("id")
        return None
    lat = dig(x, "demandStayListing", "location", "coordinate", "latitude")
    lng = dig(x, "demandStayListing", "location", "coordinate", "longitude")
    title = (dig(x, "nameLocalized", "localizedStringWithTranslationPreference")
             or dig(x, "demandStayListing", "description", "name", "localizedStringWithTranslationPreference")
             or x.get("title"))
    price = x.get("structuredDisplayPrice") or {}
    total = per_night = None
    for group in dig(price, "explanationData", "priceDetails", default=[]):
        for item in group.get("items") or []:
            desc = (item.get("description") or "").lower()
            if desc == "total":
                total = parse_money(item.get("priceString"))
            elif re.match(r"\d+ nights? x", desc):
                per_night = parse_money(desc.split(" x ", 1)[1])
    primary = price.get("primaryLine") or {}
    if total is None and (primary.get("qualifier") or "").lower() == "total":
        total = parse_money(primary.get("discountedPrice") or primary.get("price"))
    if per_night is None and total is not None and nights:
        per_night = round(total / nights, 2)
    lines = [l.get("body") or "" for l in dig(x, "structuredContent", "primaryLine", default=[])]
    lines += [l.get("body") or "" for l in dig(x, "structuredContent", "secondaryLine", default=[]) or []]
    rating = parse_money(x.get("avgRatingLocalized"))
    rc = re.search(r"\((\d[\d,]*)\)", x.get("avgRatingLocalized") or "") or \
        re.search(r"(\d[\d,]*) reviews?", x.get("avgRatingA11yLabel") or "")
    badges = []
    for b in x.get("badges") or []:
        kind = BADGES.get(dig(b, "loggingContext", "badgeType")) or (b.get("text") or "").strip().lower().replace(" ", "_")
        if kind and kind not in badges:
            badges.append(kind)
    rec = {
        "id": lid,
        "url": f"{BASE}/rooms/{lid}",
        "title": title,
        "lat": lat, "lng": lng,
        "distances_m": {},
        "near_boundary": False,
        "room_type": room_type,
        "capacity": None,  # not in search results; Airbnb filters by the guest count we send
        "bedrooms": _count(lines, r"\bbedrooms?\b|\bstudio\b"),
        "beds": _count(lines, r"\bbeds?\b"),
        "baths": _count(lines, r"bath"),
        "price_per_night": per_night,
        "price_total": total,
        "currency": None,
        "rating": rating,
        "reviews_count": int(rc.group(1).replace(",", "")) if rc else None,
        "badges": badges,
        "free_cancellation": any("cancellation" in (m.get("text") or "").lower() for m in x.get("paymentMessages") or []),
    }
    for k in ("title", "lat", "lng", "price_total", "rating"):
        if rec[k] is None:
            missing.add(k)
    return rec


# ---- crawling -----------------------------------------------------------------------------------
def guests_for_search(cfg: dict) -> tuple[int, int]:
    adults = int(cfg["guests"]["adults"])
    children = int(cfg["guests"].get("children") or 0)
    need = int(cfg.get("min_capacity") or 0)
    if adults + children < need:  # ask Airbnb for places that fit min_capacity people
        adults = need - children
    return adults, children


def zone_is_set(zone: dict | None) -> bool:
    """Both corners given and non-zero (a half-filled zone would stretch the box down to 0,0)."""
    zone = zone or {}
    return all(any((zone.get(c) or {}).get(k) for k in ("lat", "lng")) for c in ("ne", "sw"))


def trip_key(cfg: dict) -> str:
    """Prices and availability depend on dates, guests and the room-type filter: part of every cache key."""
    adults, children = guests_for_search(cfg)
    rts = sorted(rt for rt in cfg.get("room_types") or [] if rt in ROOM_TYPES)
    rt = "_" + "-".join(r.replace("_", "") for r in rts) if rts and rts != ["entire_home"] else ""
    return f"{cfg['dates']['checkin']}_{cfg['dates']['checkout']}_{adults}a{children}c_{cfg['price'].get('currency', 'USD')}{rt}"


def search_url(cfg: dict, bbox: BBox, cursor: str | None = None) -> str:
    adults, children = guests_for_search(cfg)
    params = [
        ("search_by_map", "true"), ("search_type", "user_map_move"),
        ("ne_lat", f"{bbox.ne_lat:.6f}"), ("ne_lng", f"{bbox.ne_lng:.6f}"),
        ("sw_lat", f"{bbox.sw_lat:.6f}"), ("sw_lng", f"{bbox.sw_lng:.6f}"),
        ("checkin", str(cfg["dates"]["checkin"])), ("checkout", str(cfg["dates"]["checkout"])),
        ("adults", str(adults)), ("currency", cfg["price"].get("currency", "USD")), ("locale", "en"),
    ]
    if children:
        params.append(("children", str(children)))
    for rt in cfg.get("room_types") or []:
        if rt in ROOM_TYPES:
            params.append(("room_types[]", ROOM_TYPES[rt]))
    if cursor:
        params.append(("cursor", cursor))
    return f"{BASE}/s/homes?" + urllib.parse.urlencode(params)


def crawl(f: Fetcher, cfg: dict, bbox: BBox, nights: int, room_type: str | None,
          found: dict, missing: set, depth: int = 0, stats: dict | None = None) -> None:
    stats = stats if stats is not None else {}
    html = f.page(search_url(cfg, bbox), f"search_{trip_key(cfg)}_{bbox.key()}_p0", DEFERRED_RE)
    results = search_results(extract_deferred_state(html))
    if results is None:
        log.error("search page without results JSON for box %s (structure changed or blocked)", bbox.key())
        stats["broken_pages"] = stats.get("broken_pages", 0) + 1
        return
    cursors = dig(results, "paginationInfo", "pageCursors", default=[]) or []
    total = total_hint(results)
    saturated = (total is not None and total > SEARCH_CAP) or (total is None and len(cursors) >= 15)
    if saturated and depth < MAX_DEPTH:
        log.info("%sbox %s: ~%s homes > %d, splitting", "  " * depth, bbox.key(), total, SEARCH_CAP)
        for sub in bbox.split4():
            crawl(f, cfg, sub, nights, room_type, found, missing, depth + 1, stats)
        return
    if saturated:
        log.warning("box %s still over the cap at max depth; some listings may be missed", bbox.key())
        stats["capped_boxes"] = stats.get("capped_boxes", 0) + 1
    pages = [results]
    for i, c in enumerate(cursors[1:], start=1):
        res = search_results(extract_deferred_state(f.page(search_url(cfg, bbox, c), f"search_{trip_key(cfg)}_{bbox.key()}_p{i}", DEFERRED_RE)))
        if res is None:
            log.warning("page %d of box %s unreadable, skipping", i, bbox.key())
            continue
        pages.append(res)
        if not res.get("searchResults"):
            break
    n_before = len(found)
    for res in pages:
        for x in res.get("searchResults") or []:
            rec = parse_result(x, nights, room_type, missing)
            if rec and rec["id"] not in found:
                found[rec["id"]] = rec
    log.info("%sbox %s: %s homes, %d pages, +%d new (total %d)", "  " * depth, bbox.key(), total,
             len(pages), len(found) - n_before, len(found))


def mechanical_filter(records: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    anchors = cfg.get("anchors") or []
    mode = cfg.get("geometry_mode", "all")
    max_total = float(cfg["price"].get("max_total") or 0)
    currency = cfg["price"].get("currency", "USD")
    kept, rejected = [], {"geo": 0, "price": 0, "no_coords": 0}
    for r in records:
        r["currency"] = currency
        if r["lat"] is None or r["lng"] is None:
            rejected["no_coords"] += 1
            continue
        ok, near, dist = check_anchors(r["lat"], r["lng"], anchors, mode)
        r["distances_m"], r["near_boundary"] = dist, near
        if not ok:
            rejected["geo"] += 1
            continue
        if max_total and r["price_total"] is not None and r["price_total"] > max_total:
            rejected["price"] += 1
            continue
        kept.append(r)
    kept.sort(key=lambda r: (r["near_boundary"], r["price_total"] or 1e12))
    return kept, rejected


def run(cfg: dict, data_dir: Path, f: Fetcher, force: bool = False) -> Path:
    out = data_dir / "01_search.json"
    if out.exists() and not force:
        log.info("step1: %s exists, skipping (use --force to redo)", out)
        return out
    anchors = cfg.get("anchors") or []
    for a in anchors:
        if float(a["max_distance_m"]) < 2 * FUZZ_M:
            log.warning("anchor %r: max_distance_m=%s < 300 m; Airbnb fuzzes coordinates by ~150 m, "
                        "expect many near_boundary listings", a["name"], a["max_distance_m"])
    zone = cfg.get("zone") or {}
    if zone_is_set(zone):
        bbox = BBox(zone["ne"]["lat"], zone["ne"]["lng"], zone["sw"]["lat"], zone["sw"]["lng"])
    elif anchors:
        bbox = bbox_around_anchors(anchors)
        log.info("zone not set — using box around anchors: %s", bbox.key())
    else:
        raise SystemExit("config: set zone.ne/zone.sw or at least one anchor")
    nights = (date.fromisoformat(str(cfg["dates"]["checkout"])) - date.fromisoformat(str(cfg["dates"]["checkin"]))).days
    if nights <= 0:
        raise SystemExit("config: checkout must be after checkin")
    room_types = cfg.get("room_types") or []
    room_type = room_types[0] if len(room_types) == 1 else None
    found, missing, stats = {}, set(), {}
    crawl(f, cfg, bbox, nights, room_type, found, missing, stats=stats)
    if missing:
        log.warning("fields not found in some search results (written as null): %s", ", ".join(sorted(missing)))
    kept, rejected = mechanical_filter(list(found.values()), cfg)
    log.info("step1: %d unique listings, kept %d, rejected %s, %s", len(found), len(kept), rejected, stats)
    atomic_write((data_dir / "01_search_all.json"), json.dumps(list(found.values()), ensure_ascii=False, indent=1))
    atomic_write(out, json.dumps(kept, ensure_ascii=False, indent=1))
    return out
