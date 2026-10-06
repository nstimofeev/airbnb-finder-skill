"""Parsers against real Airbnb responses captured on 2026-10-05 (tests/fixtures)."""
import json
from pathlib import Path

import pytest

from src import geo, step1_search as s1, step2_details as s2, step3_photos as s3
from src.fetch import extract_deferred_state

FX = Path(__file__).parent / "fixtures"
load = lambda n: json.loads((FX / n).read_text())


def test_extract_deferred_state():
    html = '<html><script id="data-deferred-state-0" type="application/json">{"a": 1}</script></html>'
    assert extract_deferred_state(html) == {"a": 1}
    assert extract_deferred_state("<html></html>") is None


def test_parse_money():
    assert s1.parse_money("$1,264.38") == 1264.38
    assert s1.parse_money("€1.264,38") == 1264.38
    assert s1.parse_money("₪ 980") == 980
    assert s1.parse_money(None) is None


def test_decode_listing_id():
    assert s1.decode_listing_id("RGVtYW5kU3RheUxpc3Rpbmc6MjM0MTEzOTM=") == "23411393"


def test_search_results_parse():
    res = s1.search_results(load("search_deferred.json"))
    assert len(res["searchResults"]) == 18
    assert len(res["paginationInfo"]["pageCursors"]) == 15
    assert s1.total_hint(res) == 1001  # "Over 1,000 homes"
    missing = set()
    recs = [s1.parse_result(x, 5, "entire_home", missing) for x in res["searchResults"]]
    first = recs[0]
    assert first["id"] == "23411393"
    assert first["title"] == "Trendy Apartment in the heart of Chiado"
    assert first["lat"] == pytest.approx(38.71002)
    assert first["price_total"] == 1264.38
    assert first["price_per_night"] == 243.87
    assert first["rating"] == 4.96
    assert first["badges"] == ["guest_favorite"]
    assert first["beds"] == 1 and first["baths"] == 1 and first["bedrooms"] is None
    assert first["free_cancellation"] is True
    assert "superhost" in recs[4]["badges"]
    assert recs[1]["beds"] == 2
    assert all(r["price_total"] for r in recs)
    assert missing == set()


def test_total_hint_exact():
    res = {"sectionConfiguration": {"pageTitleSections": {"sections": [
        {"sectionData": {"structuredTitle": "214 homes within map area"}}]}}}
    assert s1.total_hint(res) == 214


def test_search_url_has_bounds_dates_guests():
    cfg = {"dates": {"checkin": "2026-11-10", "checkout": "2026-11-15"}, "guests": {"adults": 2, "children": 1},
           "price": {"currency": "EUR"}, "min_capacity": 5, "room_types": ["entire_home"]}
    url = s1.search_url(cfg, geo.BBox(1.5, 2.5, 1.0, 2.0), cursor="abc")
    for part in ["ne_lat=1.500000", "sw_lng=2.000000", "checkin=2026-11-10", "adults=4", "children=1",
                 "currency=EUR", "room_types%5B%5D=Entire+home%2Fapt", "cursor=abc", "search_by_map=true"]:
        assert part in url


def test_mechanical_filter_geo_price_and_fuzz():
    anchors = [{"name": "A", "lat": 38.71, "lng": -9.14, "max_distance_m": 500}]
    cfg = {"anchors": anchors, "geometry_mode": "all", "price": {"max_total": 1000, "currency": "USD"}}
    base = dict(price_total=500, near_boundary=False, distances_m={})
    near = {**base, "id": "in", "lat": 38.7130, "lng": -9.14}      # ~334 m
    edge = {**base, "id": "edge", "lat": 38.7155, "lng": -9.14}    # ~612 m: outside, but within +150 m
    far = {**base, "id": "far", "lat": 38.7200, "lng": -9.14}      # ~1.1 km
    pricey = {**near, "id": "pricey", "price_total": 1500}
    kept, rej = s1.mechanical_filter([near, edge, far, pricey], cfg)
    assert [r["id"] for r in kept] == ["in", "edge"]
    assert kept[1]["near_boundary"] is True and kept[0]["near_boundary"] is False
    assert rej == {"geo": 1, "price": 1, "no_coords": 0}


def test_geometry_mode_any_vs_all():
    anchors = [{"name": "A", "lat": 0, "lng": 0, "max_distance_m": 1000},
               {"name": "B", "lat": 0, "lng": 1, "max_distance_m": 1000}]
    assert geo.check_anchors(0, 0.001, anchors, "all")[0] is False
    assert geo.check_anchors(0, 0.001, anchors, "any")[0] is True


def test_split4_covers_box():
    b = geo.BBox(2, 2, 0, 0)
    parts = b.split4()
    assert {(p.ne_lat, p.ne_lng, p.sw_lat, p.sw_lng) for p in parts} == {
        (2, 1, 1, 0), (2, 2, 1, 1), (1, 1, 0, 0), (1, 2, 0, 1)}


def test_bbox_around_anchors_contains_radius():
    a = [{"name": "A", "lat": 38.71, "lng": -9.14, "max_distance_m": 1000}]
    b = geo.bbox_around_anchors(a)
    h, w = b.size_m()
    assert 2200 < h < 2400 and 2200 < w < 2400


def test_parse_card():
    missing = set()
    card = s2.parse_card(load("room_deferred.json"), missing)
    assert card["description"].startswith("Location, Location, Location")
    assert {"category": "Bathroom", "name": "Hair dryer"} in [
        {k: a[k] for k in ("category", "name")} for a in card["amenities"]]
    assert card["sub_ratings"]["cleanliness"] == 4.96 and card["sub_ratings"]["location"] == 4.97
    assert any(r.startswith("Check-in after") for r in card["house_rules"])
    assert any(r.startswith("Quiet hours") for r in card["house_rules"])
    assert card["host"]["name"] == "Sam & Kim" and card["host"]["superhost"] is True
    assert card["host"]["response_rate"] == "100%"
    assert card["capacity"] == 2 and card["reviews_count"] == 394
    assert any("parking" in x.lower() for x in card["safety_and_property"])
    assert card["pets_allowed"] is False  # "No pets"
    assert missing == set()


def test_pets_allowed_variants():
    mk = lambda title: {"rules": {"groupItems": [{"items": [{"type": "HOUSE_RULES_PETS", "title": title}]}]}}
    assert s2.pets_allowed(mk("No pets")) is False
    assert s2.pets_allowed(mk("Pets allowed")) is True
    assert s2.pets_allowed(mk("2 pets maximum")) is True
    assert s2.pets_allowed({}) is None


def test_sort_options_include_lowest_rated():
    assert "RATING_ASC" in s2.review_sort_options(load("room_deferred.json"))


def test_parse_reviews_page():
    raw = load("reviews_page.json")["data"]["presentation"]["stayProductDetailPage"]["reviews"]["reviews"]
    revs = [s2.parse_review(r) for r in raw]
    assert len(revs) == 50
    assert revs[0]["rating"] == 3 and revs[0]["date"] == "2026-07-16" and revs[0]["lang"] == "en"
    assert all("<br" not in r["text"] for r in revs)
    w = s2.worst_first([{"rating": 5, "date": "2026-01-01"}, {"rating": 4, "date": "2020-01-01"},
                        {"rating": 4, "date": "2025-01-01"}, {"rating": None, "date": None}])
    assert [(r["rating"], r["date"]) for r in w][:3] == [(4, "2025-01-01"), (4, "2020-01-01"), (5, "2026-01-01")]


def test_parse_cancellation():
    summary, terms = s2.parse_cancellation(load("pdp_policies.json"))
    assert summary == "Free cancellation for 24 hours. Cancel before November 3 for a partial refund."
    assert terms[0].startswith("Full refund if cancel within 24 hours")
    assert any(t.startswith("No refund if cancel after") for t in terms)
    assert s2.parse_cancellation({}) == (None, [])


def test_parse_photos():
    photos = s3.parse_photos(load("room_deferred.json"))
    assert len(photos) > 10
    assert photos[0]["url"].startswith("https://a0.muscache.com/")
    assert photos[0]["room"] == "Living room"
    assert len({p["url"] for p in photos}) == len(photos)


def _fake_server(total, first_quirk=None, flaky_calls=0):
    """Serves reviews 0..total-1; optionally truncates answers like Airbnb did (2 of 687):
    first_quirk — only the very first call; flaky_calls — the first N calls."""
    calls = []

    def fetch(offset, limit, attempt=0):
        calls.append((offset, limit))
        n = limit
        if (first_quirk and len(calls) == 1) or len(calls) <= flaky_calls:
            n = first_quirk or 2
        items = [{"id": str(i), "rating": 3, "createdAt": "2025-01-01T00:00:00Z", "comments": "x"}
                 for i in range(offset, min(offset + n, total))]
        return items, total
    return fetch, calls


def test_collect_reviews_survives_short_first_page():
    fetch, calls = _fake_server(687, first_quirk=2)
    out, total = s2.collect_reviews(fetch, 100, True, retry_pause_s=0)
    assert total == 687 and len(out) >= 100  # fetch_reviews trims to `want` after sorting
    assert len({r["id"] for r in out}) == len(out)
    assert calls[1] == (0, 24)  # short first page retried


def test_collect_reviews_long_flaky_streak_still_gets_100():
    # 15 truncated answers in a row (seen live on listing 18752967: 30 of 190 collected)
    fetch, calls = _fake_server(190, flaky_calls=15)
    out, _ = s2.collect_reviews(fetch, 100, True, retry_pause_s=0)
    assert len(out) >= 100


def test_collect_reviews_stops_at_total():
    fetch, calls = _fake_server(5)
    out, _ = s2.collect_reviews(fetch, 100, True, retry_pause_s=0)
    assert len(out) == 5 and len(calls) == 1


def test_collect_reviews_unsorted_reads_everything():
    fetch, _ = _fake_server(230)
    out, _ = s2.collect_reviews(fetch, 100, False, retry_pause_s=0)
    assert len(out) == 230


def test_quote_check():
    from src import model_steps as ms
    src = ["Leider gibt’s in dem Haus viele Wohnungen. Zudem gibt’s viel Lärm von den Obdachlosen in der Nacht.",
           "O espaço estava impecavelmente limpo e organizado."]
    assert ms.quote_found("(2024) Zudem gibt's viel Lärm von den Obdachlosen in der Nacht [ночью шумно]", src)
    assert not ms.quote_found("(2026) the place was impecavelmente limpo [безупречно чисто]", src)
    assert not ms.quote_found("Totally made up sentence about mold", src)
    det = {"amenities": [{"category": "Not included", "name": "Air conditioning", "available": False}]}
    assert ms.quote_found("Air conditioning (Not included)", ms.listing_sources(det))


def test_review_digest_greps_by_stems_in_several_languages():
    from src import model_steps as ms
    pats = ms.keyword_patterns({"restaurants": ["restoran", "ресторан", "restaurant"], "road": ["makadam", "gravel", "road"]})
    reviews = [
        {"rating": 5, "date": "2025-09-01", "lang": "sr", "text": "Kuća je divna. Restorani su blizu, oko 10 minuta.", "host_reply": None},
        {"rating": 2, "date": "2024-10-10", "lang": "en", "text": "The road is steep gravel! We barely made it.", "host_reply": "We fixed it."},
        {"rating": 4, "date": "2025-08-01", "lang": "ru", "text": "Рядом хороший ресторан. Дом чистый.", "host_reply": None},
        {"rating": 5, "date": "2025-07-01", "lang": "en", "text": "Lovely view, very quiet.", "host_reply": None},
    ]
    d = ms.review_digest(reviews, pats)
    assert [h["text"] for h in d["snippets"]["restaurants"]] == ["Рядом хороший ресторан.", "Restorani su blizu, oko 10 minuta."]
    assert d["snippets"]["road"][0]["text"] == "The road is steep gravel!" and d["snippets"]["road"][0]["host_reply"] == "We fixed it."
    assert d["mentions_per_criterion"] == {"restaurants": 2, "road": 1}
    assert [r["rating"] for r in d["low_rated_reviews"]] == [2]
    assert d["stats"]["collected"] == 4 and d["stats"]["by_stars"]["5"] == 2
    # stems match only at the start of a word
    assert not ms.keyword_patterns({"x": ["road"]})["x"].search("railroad")
    car = ms.keyword_patterns({"x": ["car", "parking"]})["x"]
    assert car.search("we left the car below") and not car.search("lovely carpet") and car.search("Parkingu")


def test_cache_key_depends_on_dates_and_guests():
    base = {"dates": {"checkin": "2026-10-19", "checkout": "2026-10-22"}, "guests": {"adults": 2}, "price": {"currency": "EUR"}}
    other = {**base, "dates": {"checkin": "2026-10-27", "checkout": "2026-10-30"}}
    more = {**base, "guests": {"adults": 3}}
    assert len({s1.trip_key(base), s1.trip_key(other), s1.trip_key(more)}) == 3


def test_same_brand_cluster_detects_sibling_cabins():
    from src import model_steps as ms
    me = {"id": "1", "title": "Pine Ridge Pure Nature Cab 1.", "lat": 43.880, "lng": 19.416}
    seen = {"1": me,
            "2": {"id": "2", "title": "Pine Ridge Pure Nature\nCab 2.", "lat": 43.879, "lng": 19.417},
            "3": {"id": "3", "title": "Log Cabin Luna in the National Park", "lat": 43.8805, "lng": 19.4165},
            "4": {"id": "4", "title": "Pure Nature Lodge far away", "lat": 43.95, "lng": 19.50}}
    r = ms.neighbour_listings("1", me, seen)
    assert r["airbnb_listings_within_400m_any_dates"] == 2
    assert r["same_brand_nearby"] == ["Pine Ridge Pure Nature\nCab 2."]


def test_pets_only_none_drops_allowed_keeps_unknown(tmp_path):
    from src import model_steps as ms
    (tmp_path / "03_details").mkdir()
    for i, pets in [("a", False), ("b", True), ("c", None)]:
        (tmp_path / "03_details" / f"{i}.json").write_text(json.dumps({"pets_allowed": pets}))
    (tmp_path / "02_shortlist.json").write_text(json.dumps({"ids": ["a", "b", "c"]}))
    assert ms.shortlist_ids({"pets": "only_none"}, tmp_path) == ["a", "c"]
    assert ms.shortlist_ids({"pets": "prefer_none"}, tmp_path) == ["a", "b", "c"]


def test_hard_criteria_first_three_plus_config(tmp_path):
    from src import model_steps as ms
    crit = tmp_path / "criteria.md"
    crit.write_text("### a\nx\n### b\nx\n### c\nx\n### d\nx\n### no_pets\nx\n")
    ms.CRITERIA_FILE = crit
    try:
        assert ms.hard_criteria({"hard_criteria": ["no_pets", "zzz"]}, tmp_path) == ["a", "b", "c", "no_pets"]
    finally:
        ms.CRITERIA_FILE = None


def test_drop_villages_by_buildings_listings_and_brand():
    from src import model_steps as ms
    search = {"a": {"id": "a", "title": "Lonely Cabin", "lat": 43.90, "lng": 19.40},
              "b": {"id": "b", "title": "Village House", "lat": 43.95, "lng": 19.45},
              "c": {"id": "c", "title": "Pure Nature Cab 1", "lat": 43.80, "lng": 19.30}}
    seen = {**search, "c2": {"id": "c2", "title": "Pure Nature Cab 2", "lat": 43.8005, "lng": 19.3005}}
    nb = lambda b150, b300: {"neighbours": {"osm_buildings_within_150m": b150, "osm_buildings_within_300m": b300}}
    ctx = {"a": nb(1, 4), "b": nb(12, 40), "c": nb(0, 1)}
    kept = ms.drop_villages(search, ctx, seen, {"max_buildings_150m": 5, "max_buildings_300m": 15, "max_airbnb_400m": 3})
    assert list(kept) == ["a"]   # b — village by buildings, c — sibling cabin of the same brand


def test_must_pass_replaces_default_hard_criteria(tmp_path):
    from src import model_steps as ms
    crit = tmp_path / "criteria.md"
    crit.write_text("### a\nx\n### b\nx\n### c\nx\n### no_pets\nx\n")
    ms.CRITERIA_FILE = crit
    try:
        assert ms.hard_criteria({"must_pass": ["a", "no_pets"], "hard_criteria": ["c"]}, tmp_path) == ["a", "no_pets"]
    finally:
        ms.CRITERIA_FILE = None
