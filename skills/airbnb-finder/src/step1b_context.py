"""Step 1b: surroundings of every listing that passed step 1 → data/01b_context.json.

Free sources, no keys:
  - OpenStreetMap via Overpass: restaurants, viewpoints/peaks/trailheads/hiking routes, buildings, roads;
  - OSRM public demo server: driving times (table) and the route to a hub (for road surface).
Code only measures; judging "secluded", "good access" etc. is the model's job.
Caveats (also told to the model): listing coordinates are fuzzed ~150 m; OSM is incomplete in rural
areas (missing restaurants, missing `surface` tags); OSRM times are free-flow estimates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import urllib.parse
from pathlib import Path

from .fetch import Fetcher, HTTPStatusError, NetworkError, atomic_write
from .geo import EARTH_R, BBox, bbox_around_anchors, haversine_m
from .step1_search import zone_is_set

log = logging.getLogger(__name__)

OVERPASS = "https://overpass-api.de/api/interpreter?data="
OSRM = "https://router.project-osrm.org"
FOOD = {"restaurant", "pub", "fast_food", "cafe", "biergarten"}
MEAL = {"restaurant", "pub", "biergarten"}  # places to have lunch/dinner (a cafe is often drinks only)
PAVED = {"asphalt", "paved", "concrete", "concrete:plates", "concrete:lanes", "paving_stones", "sett", "chipseal"}
UNPAVED = {"unpaved", "gravel", "fine_gravel", "compacted", "dirt", "ground", "earth", "grass", "mud", "sand",
           "pebblestone", "rock", "woodchips"}
MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link", "primary_link",
         "secondary_link", "tertiary_link"}


# ---- pure helpers ------------------------------------------------------------------------------
def pad_bbox(b: BBox, metres: float) -> BBox:
    dlat = math.degrees(metres / EARTH_R)
    dlng = math.degrees(metres / (EARTH_R * math.cos(math.radians((b.ne_lat + b.sw_lat) / 2))))
    return BBox(b.ne_lat + dlat, b.ne_lng + dlng, b.sw_lat - dlat, b.sw_lng - dlng)


def ov_bbox(b: BBox) -> str:
    return f"{b.sw_lat:.5f},{b.sw_lng:.5f},{b.ne_lat:.5f},{b.ne_lng:.5f}"


def surface_class(tags: dict) -> str:
    """paved | unpaved | unknown for one OSM way."""
    s = (tags.get("surface") or "").lower()
    if s in PAVED:
        return "paved"
    if s in UNPAVED or tags.get("highway") == "track":
        return "unpaved"
    if tags.get("tracktype") and tags["tracktype"] != "grade1":
        return "unpaved"
    if tags.get("highway") in MAJOR:
        return "paved"  # main roads without a surface tag are paved in practice
    return "unknown"


def edge_index(ways: list[dict]) -> dict[tuple[int, int], dict]:
    """(node_a, node_b) -> way tags, both directions, for mapping an OSRM node path back to OSM ways."""
    idx = {}
    for w in ways:
        nds, tags = w.get("nodes") or [], w.get("tags") or {}
        for a, b in zip(nds, nds[1:]):
            idx[(a, b)] = tags
            idx[(b, a)] = tags
    return idx


def route_surface(nodes: list[int], dists: list[float], idx: dict, first_m: float = 5000) -> dict:
    """Metres of paved/unpaved/unknown road along the first `first_m` metres of a route."""
    out = {"paved": 0.0, "unpaved": 0.0, "unknown": 0.0}
    done = 0.0
    for (a, b), d in zip(zip(nodes, nodes[1:]), dists):
        if done >= first_m:
            break
        d = min(d, first_m - done)
        tags = idx.get((a, b))
        out[surface_class(tags) if tags is not None else "unknown"] += d
        done += d
    return {k: round(v) for k, v in out.items()}


def count_within(lat: float, lng: float, pts: list[tuple[float, float]], radius: float) -> int:
    return sum(1 for p in pts if haversine_m(lat, lng, p[0], p[1]) <= radius)


# ---- network -----------------------------------------------------------------------------------
def overpass_ok(data: dict) -> bool:
    """Overpass reports timeouts and memory limits inside a 200 answer; such partial data must not be cached."""
    remark = str(data.get("remark") or "").lower()
    return not any(w in remark for w in ("error", "timed out", "timeout", "out of memory"))


def overpass(f: Fetcher, query: str, key: str) -> list[dict]:
    """Elements for the query; on persistent trouble an empty list and a warning, not a crash."""
    q = "[out:json][timeout:180];" + query
    try:
        data = f.get_json(OVERPASS + urllib.parse.quote(q), f"{key}_{hashlib.sha1(q.encode()).hexdigest()[:10]}",
                          delay=(1.0, 2.0), timeout=210, valid=overpass_ok)
    except (NetworkError, HTTPStatusError) as e:
        log.warning("OpenStreetMap (Overpass) unavailable for %s: %s — surroundings will be incomplete; "
                    "rerun `context --force` later", key, e)
        return []
    if not overpass_ok(data):
        log.warning("Overpass %s: %s — surroundings incomplete; rerun `context --force` later", key, data.get("remark"))
    return data.get("elements") or []


def osrm_table(f: Fetcher, src: tuple[float, float], dests: list[tuple[float, float]], key: str) -> tuple[list, float | None]:
    """Durations (s) and distances (m) from src to each dest; plus how far src had to snap to a road."""
    durations, distances, snap = [], [], None
    for i in range(0, len(dests), 90):
        chunk = dests[i:i + 90]
        coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in [src] + chunk)
        url = f"{OSRM}/table/v1/driving/{coords}?sources=0&annotations=duration,distance"
        # the cache key must depend on the exact points, or a changed point set would reuse stale times
        d = f.get_json(url, f"{key}_{i}_{hashlib.sha1(coords.encode()).hexdigest()[:10]}", return_statuses=(400,),
                       valid=lambda d: d.get("code") == "Ok")
        if d.get("code") != "Ok":  # e.g. NoSegment: the point is far from any road
            log.warning("OSRM table %s: %s %s — no driving times for this listing", key, d.get("code"), d.get("message"))
            durations += [None] * len(chunk)
            distances += [None] * len(chunk)
            continue
        durations += d["durations"][0][1:]
        distances += d["distances"][0][1:]
        snap = (d.get("sources") or [{}])[0].get("distance", snap)
    return list(zip(durations, distances)), snap


def osrm_route_nodes(f: Fetcher, src: tuple[float, float], dst: tuple[float, float], key: str):
    url = (f"{OSRM}/route/v1/driving/{src[1]:.6f},{src[0]:.6f};{dst[1]:.6f},{dst[0]:.6f}"
           f"?overview=false&annotations=nodes,distance")
    d = f.get_json(url, key, return_statuses=(400,))  # OSRM answers NoRoute with HTTP 400
    if d.get("code") != "Ok" or not d.get("routes"):
        return None
    ann = d["routes"][0]["legs"][0]["annotation"]
    return ann["nodes"], ann["distance"], d["routes"][0]["duration"], d["routes"][0]["distance"]


# ---- main --------------------------------------------------------------------------------------
def zone_bbox(cfg: dict) -> BBox:
    zone = cfg.get("zone") or {}
    if zone_is_set(zone):
        return BBox(zone["ne"]["lat"], zone["ne"]["lng"], zone["sw"]["lat"], zone["sw"]["lng"])
    return bbox_around_anchors(cfg.get("anchors") or [])


CELL_DEG = 0.05  # ~5 km


def grid_cells(pts: list[tuple[float, float]]) -> set[tuple[int, int]]:
    return {(math.floor(lat / CELL_DEG), math.floor(lng / CELL_DEG)) for lat, lng in pts}


def cell_bbox(cell: tuple[int, int], pad_m: float) -> BBox:
    b = BBox((cell[0] + 1) * CELL_DEG, (cell[1] + 1) * CELL_DEG, cell[0] * CELL_DEG, cell[1] * CELL_DEG)
    return pad_bbox(b, pad_m)


def poi_name(t: dict) -> str | None:
    return t.get("name:en") or t.get("name")


def run(cfg: dict, data_dir: Path, f: Fetcher, force: bool = False) -> Path:
    out = data_dir / "01b_context.json"
    ctx_cfg = cfg.get("context") or {}
    if ctx_cfg.get("enabled") is False:
        log.info("context: disabled in config")
        atomic_write(out, "{}")
        return out
    # surroundings depend on the places, the area and the radius: recompute everything when they change
    fp = hashlib.sha1(json.dumps([cfg.get("places"), cfg.get("zone"), cfg.get("anchors"), ctx_cfg.get("poi_radius_km")],
                                 sort_keys=True, default=str).encode()).hexdigest()[:12]
    fp_file = data_dir / "01b_context.inputs"
    if out.exists() and fp_file.exists() and fp_file.read_text().strip() != fp and not force:
        log.info("context: places/area/radius changed since the last run — recomputing surroundings")
        force = True
    have = json.loads(out.read_text()) if out.exists() and not force else {}
    atomic_write(fp_file, fp)
    listings = json.loads((data_dir / "01_search.json").read_text())
    todo = [l for l in listings if l["id"] not in have and l["lat"] is not None]
    if not todo:
        log.info("context: all %d listings done", len(listings))
        return out
    zone = zone_bbox(cfg)
    # regional trips (a zone) look 30 km around for trails and food; city trips (anchors only) 5 km
    default_km = 30 if zone_is_set(cfg.get("zone")) else 5
    reach_m = float(ctx_cfg.get("poi_radius_km") or default_km) * 1000
    wide, near = pad_bbox(zone, reach_m), pad_bbox(zone, 3000)

    food = [e for e in overpass(f, f'nwr["amenity"~"^({"|".join(FOOD)})$"]({ov_bbox(wide)});out tags center;', "ov_food")]
    nature = overpass(f, f'(nwr["tourism"="viewpoint"]({ov_bbox(wide)});node["natural"~"^(peak|waterfall)$"]["name"]({ov_bbox(wide)});'
                         f'nwr["highway"="trailhead"]({ov_bbox(wide)});relation["route"="hiking"]({ov_bbox(wide)}););out tags center;',
                      "ov_nature")
    listings_ll = [(l["lat"], l["lng"]) for l in todo]
    buildings = {}  # by OSM id: padded cells overlap
    for cell in sorted(grid_cells(listings_ll)):  # per cell: a whole city's buildings in one query is too heavy
        for e in overpass(f, f'way["building"]({ov_bbox(cell_bbox(cell, 400))});out center;',
                          f"ov_buildings_{cell[0]}_{cell[1]}"):
            buildings[e["id"]] = e
    buildings = list(buildings.values())
    hub = (cfg.get("places") or [None])[0]
    # roads are only needed to judge the road in from the first place (rural trips)
    ways = overpass(f, f'way["highway"]({ov_bbox(near)});out body;', "ov_roads") if hub else []
    log.info("context: OSM — %d food places, %d nature points, %d buildings, %d road ways",
             len(food), len(nature), len(buildings), len(ways))

    def latlng(e):
        c = e.get("center") or e
        return (c.get("lat"), c.get("lon"))

    food_pts = [{"name": poi_name(e["tags"]), "kind": e["tags"].get("amenity"), "ll": latlng(e)} for e in food]
    nat_pts = []
    for e in nature:
        t = e["tags"]
        kind = ("hiking_route" if t.get("route") == "hiking" else t.get("tourism") or t.get("natural") or t.get("highway"))
        nat_pts.append({"name": poi_name(t), "kind": kind, "ll": latlng(e)})
    places = [{"name": p["name"], "kind": "place", "ll": (p["lat"], p["lng"])} for p in cfg.get("places") or []]
    bpts = [latlng(e) for e in buildings if latlng(e)[0] is not None]
    all_listings = json.loads((data_dir / "01_search_all.json").read_text())
    lpts = {l["id"]: (l["lat"], l["lng"]) for l in all_listings if l.get("lat") is not None}
    idx = edge_index(ways)

    for n, l in enumerate(todo, 1):
        here = (l["lat"], l["lng"])
        cand_food = sorted((p for p in food_pts if p["ll"][0] is not None),
                           key=lambda p: haversine_m(*here, *p["ll"]))[:60]
        by_dist = sorted((p for p in nat_pts if p["ll"][0] is not None), key=lambda p: haversine_m(*here, *p["ll"]))
        cand_nat = [p for p in by_dist if p["kind"] != "peak"][:40] + [p for p in by_dist if p["kind"] == "peak"][:15]
        dests = cand_food + cand_nat + places
        try:
            res, snap = osrm_table(f, here, [p["ll"] for p in dests], f"osrm_table_{l['id']}")
        except (NetworkError, HTTPStatusError) as e:  # not stored: the next run tries this listing again
            log.warning("routing server unavailable for %s: %s", l["id"], e)
            continue
        timed = []
        for p, (dur, dist) in zip(dests, res):
            if dur is None:
                continue
            timed.append({**p, "min": round(dur / 60, 1), "km": round(dist / 1000, 1)})
        tf = sorted((p for p in timed if p["kind"] in FOOD), key=lambda p: p["min"])
        tn = sorted((p for p in timed if p["kind"] not in FOOD and p["kind"] != "place"), key=lambda p: p["min"])
        meal = [p for p in tf if p["kind"] in MEAL]
        rec = {
            "snap_to_road_m": round(snap) if snap is not None else None,
            "food": {
                "meal_places_within_10min": sum(p["min"] <= 10 for p in meal),
                "meal_places_within_15min": sum(p["min"] <= 15 for p in meal),
                "meal_places_within_20min": sum(p["min"] <= 20 for p in meal),
                "nearest": [{k: p[k] for k in ("name", "kind", "min", "km")} for p in tf[:8]],
            },
            "nature": {
                "viewpoints_within_25min": sum(p["min"] <= 25 and p["kind"] == "viewpoint" for p in tn),
                "hiking_routes_within_25min": sum(p["min"] <= 25 and p["kind"] in ("hiking_route", "trailhead") for p in tn),
                "peaks_within_25min": sum(p["min"] <= 25 and p["kind"] == "peak" for p in tn),
                # peaks are everywhere in the mountains and drown the list; keep viewpoints, routes, waterfalls
                "nearest": [{k: p[k] for k in ("name", "kind", "min", "km")} for p in tn if p["kind"] != "peak"][:10],
            },
            "places_min": {p["name"]: p["min"] for p in timed if p["kind"] == "place"},
            "neighbours": {
                "osm_buildings_within_150m": count_within(*here, bpts, 150),
                "osm_buildings_within_300m": count_within(*here, bpts, 300),
                "airbnb_listings_within_300m": sum(1 for i, p in lpts.items() if i != l["id"] and haversine_m(*here, *p) <= 300),
            },
            "access": None,
        }
        if hub:
            try:
                r = osrm_route_nodes(f, here, (hub["lat"], hub["lng"]), f"osrm_route_{l['id']}_{hub['lat']:.4f}_{hub['lng']:.4f}")
            except (NetworkError, HTTPStatusError) as e:
                log.warning("route to %s unavailable for %s: %s", hub["name"], l["id"], e)
                r = None
            if r:
                nodes, dists, dur, dist = r
                rec["access"] = {"to": hub["name"], "min": round(dur / 60, 1), "km": round(dist / 1000, 1),
                                 "first_5km_m": route_surface(nodes, dists, idx)}
        have[l["id"]] = rec
        if n % 20 == 0 or n == len(todo):
            atomic_write(out, json.dumps(have, ensure_ascii=False, indent=1))
            log.info("context [%d/%d]", n, len(todo))
    atomic_write(out, json.dumps(have, ensure_ascii=False, indent=1))
    return out
