"""Step 2: listing card + worst reviews for shortlisted IDs → data/03_details/<id>.json."""
from __future__ import annotations

import base64
import html as htmllib
import json
import logging
import re
import time
from pathlib import Path

from .fetch import BASE, Fetcher, GraphQLError, dig, extract_deferred_state, niobe_entry
from .ophash import OpHashes
from .step1_search import trip_key

log = logging.getLogger(__name__)

REVIEWS_PAGE = 50  # the API caps a page at 50
FIRST_PAGE = 24  # what the site asks for first
MAX_REVIEW_PAGES_UNSORTED = 30  # fallback when "lowest rated" sort is unavailable: read up to 1500, sort locally
SUB_RATINGS = {
    "cleanlinessRating": "cleanliness", "accuracyRating": "accuracy", "checkinRating": "checkin",
    "communicationRating": "communication", "locationRating": "location", "valueRating": "value",
}


def strip_html(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>\s*<p>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return htmllib.unescape(s).strip()


def room_url(lid: str, cfg: dict) -> str:
    from .step1_search import guests_for_search
    adults, children = guests_for_search(cfg)
    q = (f"check_in={cfg['dates']['checkin']}&check_out={cfg['dates']['checkout']}&adults={adults}"
         f"&currency={cfg['price'].get('currency', 'USD')}&locale=en")
    if children:
        q += f"&children={children}"
    return f"{BASE}/rooms/{lid}?{q}"


def global_id(kind: str, lid: str) -> str:
    return base64.b64encode(f"{kind}:{lid}".encode()).decode()


# ---- card (pure) --------------------------------------------------------------------------------
def pdp_parts(state: dict | None) -> tuple[dict, dict]:
    """(pdpPresentation node, sections metadata) from a listing page's cache."""
    resp = niobe_entry(state, "StaysPdpSections") or {}
    node = dig(resp, "data", "node", "pdpPresentation", default={})
    meta = dig(resp, "data", "presentation", "stayProductDetailPage", "sections", "metadata", default={})
    return node, meta


def parse_card(state: dict | None, missing: set) -> dict:
    node, meta = pdp_parts(state)
    ev = dig(meta, "loggingContext", "eventDataLogging", default={})
    sharing = meta.get("sharingConfig") or {}

    desc = dig(node, "descriptions", "longDescriptionHtml", "localizedStringWithTranslationPreference") \
        or dig(node, "descriptions", "longDescriptionHtml", "localizedString")
    amenities = []
    for g in dig(node, "amenities", "seeAllAmenitiesGroups", default=[]):
        for a in g.get("amenities") or []:
            name = a.get("title")
            if not name:
                continue
            item = {"category": g.get("title"), "name": name}
            sub = dig(a, "subtitle", "text")
            if sub:
                item["note"] = sub
            if a.get("available") is False:
                item["available"] = False
            amenities.append(item)

    sub_ratings = {v: ev.get(k) for k, v in SUB_RATINGS.items()}

    def flatten(groups):
        out = []
        for g in groups or []:
            for it in g.get("items") or []:
                t = it.get("title")
                d = dig(it, "description", "text") or dig(it, "description", "content", "localizedStringWithTranslationPreference")
                if t:
                    out.append(f"{t}: {d.strip()}" if d else t)
        return out

    host_info = node.get("hostInfo") or {}
    passport = host_info.get("passportData") or {}
    card = {
        "description": strip_html(desc),
        "amenities": amenities,
        "sub_ratings": sub_ratings,
        "house_rules": flatten(dig(node, "rules", "groupItems")),
        "safety_and_property": flatten(dig(node, "safetyAndProperty", "groupItems")),
        "host": {
            "name": passport.get("name"),
            "superhost": passport.get("isSuperhost"),
            "response_rate": (host_info.get("responseRateText") or "").replace("Response rate:", "").strip() or None,
            "response_time": host_info.get("responseTimeText"),
            "years_hosting": dig(passport, "timeAsHost", "years"),
            "host_rating": passport.get("ratingAverage"),
            "host_reviews": passport.get("ratingCount"),
            "about": dig(host_info, "about", "localizedStringWithTranslationPreference"),
        },
        "cancellation_policy": None,
        "pets_allowed": pets_allowed(node),
        "property_type": sharing.get("propertyType"),
        "capacity": node.get("personCapacity") or ev.get("personCapacity") or sharing.get("personCapacity"),
        "overview": dig(node, "overview", "items", default=[]),
        "rating": ev.get("guestSatisfactionOverall") or sharing.get("starRating"),
        "reviews_count": sharing.get("reviewCount") or (int(ev["visibleReviewCount"]) if str(ev.get("visibleReviewCount") or "").isdigit() else None),
        "exact_location": dig(node, "location", "isExactLocation"),
    }
    for k in ("description", "amenities", "house_rules", "capacity"):
        if not card[k]:
            missing.add(k)
    if not any(sub_ratings.values()):
        missing.add("sub_ratings")
    if not card["host"]["name"]:
        missing.add("host")
    return card


def pets_allowed(node: dict) -> bool | None:
    """From the HOUSE_RULES_PETS rule: 'No pets' -> False, 'Pets allowed' / 'N pets maximum' -> True."""
    for g in dig(node, "rules", "groupItems", default=[]):
        for it in g.get("items") or []:
            if it.get("type") == "HOUSE_RULES_PETS" or re.search(r"\bpets?\b", (it.get("title") or "").lower()):
                t = (it.get("title") or "").lower()
                if re.search(r"\bno pets\b|pets not allowed|not allowed", t):
                    return False
                if re.search(r"allowed|maximum|\d+ pets?", t):
                    return True
    return None


def review_sort_options(state: dict | None) -> set[str]:
    return set(re.findall(r'"reviewsSortType":\s*"([A-Z_]+)"', json.dumps(state or {})))


def parse_cancellation(policies: dict) -> tuple[str | None, list[str]]:
    """From StaysPdpPoliciesQuery: (one-line summary, refund timeline lines)."""
    node = dig(policies, "data", "node", "pdpPresentation", default={})
    summary = None
    for t in node.get("thingsToKnow") or []:
        if t.get("type") == "CANCELLATION_POLICY":
            items = [strip_html(x) for x in t.get("previewItems") or [] if x and not x.startswith("Review this host")]
            summary = " ".join(items) or None
    terms = [strip_html(e.get("accessibilityContent")).rstrip(".") + "."
             for e in dig(node, "cancellationPolicy", "entries", default=[]) if e.get("accessibilityContent")]
    return summary, terms


def parse_review(r: dict) -> dict:
    created = r.get("createdAt") or ""
    resp = r.get("response")
    if isinstance(resp, dict):
        resp = resp.get("comments") or resp.get("response") or resp.get("text")
    return {
        "rating": r.get("rating"),
        "date": created[:10] or None,
        "lang": r.get("language"),
        "text": strip_html(r.get("comments") or r.get("commentV2") or ""),
        "host_reply": strip_html(resp) if isinstance(resp, str) and resp.strip() else None,
        "id": r.get("id"),
    }


def worst_first(reviews: list[dict]) -> list[dict]:
    return sorted(reviews, key=lambda r: (r["rating"] if r["rating"] is not None else 99, "" if not r["date"] else
                                          "".join(chr(255 - ord(c)) for c in r["date"])))


# ---- network ------------------------------------------------------------------------------------
def collect_reviews(fetch_page, want: int, sorted_worst_first: bool, short_retries: int = 2,
                    retry_pause_s: float = 3.0) -> tuple[list[dict], int | None]:
    """Page through reviews by offset = number actually received.

    Airbnb intermittently answers with a truncated page (seen: 2 reviews where 24/50 were asked, for a
    listing with 190–687 reviews; the same request from a fresh session returns the full page). So a short
    page is retried with a fresh session (see fetch_page), and if it stays short we still move on by what
    arrived. Only an empty page,
    no new reviews, or reaching the total ends the loop. The site itself starts with 24, then 50.
    fetch_page(offset, limit, attempt) -> (raw reviews, total or None).
    """
    out, seen, total, offset = [], set(), None, 0
    cap = want if sorted_worst_first else REVIEWS_PAGE * MAX_REVIEW_PAGES_UNSORTED
    for i in range(cap // 10 + 10):  # hard stop on requests, not on reviews
        limit = FIRST_PAGE if i == 0 else REVIEWS_PAGE
        for attempt in range(short_retries + 1):
            batch, t = fetch_page(offset, limit, attempt)
            total = t if t is not None else total
            expected = limit if total is None else min(limit, max(total - offset, 0))
            if len(batch) >= expected or attempt == short_retries:
                break
            log.info("short reviews page (%d of %d at offset %d), retrying", len(batch), expected, offset)
            time.sleep(retry_pause_s)
        new = 0
        for r in batch:
            pr = parse_review(r)
            if pr["id"] not in seen:
                seen.add(pr["id"])
                out.append(pr)
                new += 1
        offset += len(batch)
        if not batch or not new or len(out) >= cap or (total is not None and len(out) >= total):
            break
    return out, total


def fetch_reviews(f: Fetcher, hashes: OpHashes, lid: str, cfg: dict, page_html: str, want: int,
                  sort_options: set[str]) -> tuple[list[dict], dict]:
    sort = "RATING_ASC" if "RATING_ASC" in sort_options or not sort_options else None

    def fetch_page(offset: int, limit: int, attempt: int = 0):
        variables = {"id": global_id("StayListing", lid), "pdpReviewsRequest": {
            "fieldSelector": "for_p3_translation_only", "forPreview": False, "limit": limit,
            "offset": str(offset), "showingTranslationButton": False, "first": limit,
            "sortingPreference": sort or "MOST_RECENT",
            "checkinDate": str(cfg["dates"]["checkin"]), "checkoutDate": str(cfg["dates"]["checkout"]),
            "numberOfAdults": str(cfg["guests"]["adults"]), "numberOfChildren": str(cfg["guests"].get("children") or 0),
            "numberOfInfants": "0", "numberOfPets": "0"}}
        if attempt:
            f.reset_session()
        key = f"reviews_{lid}_{sort or 'RECENT'}_o{offset}_l{limit}" + (f"_r{attempt}" if attempt else "")
        try:
            data = f.graphql("StaysPdpReviewsQuery", hashes.get("StaysPdpReviewsQuery"), variables, key)
        except GraphQLError as e:
            if "PersistedQueryNotFound" not in json.dumps(e.errors) and "persisted" not in str(e).lower():
                raise
            hashes.rediscover("StaysPdpReviewsQuery", page_html)
            data = f.graphql("StaysPdpReviewsQuery", hashes.get("StaysPdpReviewsQuery"), variables, key)
        block = dig(data, "data", "presentation", "stayProductDetailPage", "reviews", default={})
        batch, total = block.get("reviews") or [], dig(block, "metadata", "reviewsCount")
        if total is not None and len(batch) < min(limit, max(total - offset, 0)):
            f.forget(key)  # truncated answer: don't let it poison the next run
        return batch, total

    out, total = collect_reviews(fetch_page, want, bool(sort))
    out = worst_first(out)[:want]
    for r in out:
        r.pop("id", None)
    return out, {"sort": sort or "MOST_RECENT+local", "total": total}


def fetch_cancellation(f: Fetcher, hashes: OpHashes, lid: str, cfg: dict, page_html: str) -> tuple[str | None, list[str]]:
    variables = {"id": global_id("DemandStayListing", lid), "checkIn": str(cfg["dates"]["checkin"]),
                 "checkOut": str(cfg["dates"]["checkout"]), "selectedCancellationPolicyId": None,
                 "includeVettedHouseRulesFields": False}
    op = "StaysPdpPoliciesQuery"
    try:
        try:
            data = f.graphql(op, hashes.get(op), variables, f"policies_{lid}_{trip_key(cfg)}")
        except GraphQLError:
            hashes.rediscover(op, page_html)
            data = f.graphql(op, hashes.get(op), variables, f"policies_{lid}_{trip_key(cfg)}")
    except Exception as e:
        log.warning("%s: cancellation policy unavailable: %s", lid, e)
        return None, []
    return parse_cancellation(data)


def niobe_entry_key(state: dict | None, prefix: str) -> str | None:
    for entry in (state or {}).get("niobeClientData", []) or []:
        if isinstance(entry, list) and entry and str(entry[0]).startswith(prefix):
            return entry[0]
    return None


def run(cfg: dict, data_dir: Path, f: Fetcher, force: bool = False) -> Path:
    shortlist = json.loads((data_dir / "02_shortlist.json").read_text())
    ids = [str(i) for i in shortlist["ids"]]
    out_dir = data_dir / "03_details"
    out_dir.mkdir(exist_ok=True)
    hashes = OpHashes(f, data_dir / "raw" / "ophashes.json")
    want = int(cfg.get("reviews_per_listing") or 100)
    for n, lid in enumerate(ids, 1):
        out = out_dir / f"{lid}.json"
        if out.exists() and not force:
            continue
        html = f.page(room_url(lid, cfg), f"room_{lid}_{trip_key(cfg)}")
        state = extract_deferred_state(html)
        missing: set = set()
        card = parse_card(state, missing)
        card["cancellation_policy"], card["cancellation_terms"] = fetch_cancellation(f, hashes, lid, cfg, html)
        if card["cancellation_policy"] is None:
            missing.add("cancellation_policy")
        reviews, meta = fetch_reviews(f, hashes, lid, cfg, html, want, review_sort_options(state))
        rec = {"id": lid, **card, "reviews_meta": {"collected": len(reviews), **meta}, "reviews": reviews}
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        log.info("step2 [%d/%d] %s: %d reviews (of %s, sort %s)%s", n, len(ids), lid, len(reviews), meta["total"],
                 meta["sort"], f"; missing: {', '.join(sorted(missing))}" if missing else "")
    return out_dir
