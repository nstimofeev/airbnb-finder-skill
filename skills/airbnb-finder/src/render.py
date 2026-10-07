"""Build output/:
  index.html + assets/ + photos/ + tiles/ — the page, works offline and from any static web host;
  <trip>-page.zip — the same folder in one file, to send to someone;
  artifact.html + manifest-artifact.json — a smaller copy for publishing as a claude.ai artifact.

Photos and OpenStreetMap tiles are downloaded into the bundle and referenced by relative paths: claude.ai
artifacts block images from other hosts, and the page then works without internet.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

from .fetch import atomic_write, Fetcher
from .localize import localize
from .model import ModelError, call
from .model_steps import language_name, mark_unconfirmed
from .step3_photos import caption_text
from .geo import EARTH_R, bbox_around_anchors

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "finalists.html"
VENDOR = ROOT / "templates" / "vendor" / "leaflet"  # Leaflet 1.9.4, BSD-2 (see LICENSE there)
OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


# ---- map tiles ----------------------------------------------------------------------------------
def tile_xy(lat: float, lng: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lng + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_for(bounds: tuple[float, float, float, float], z: int) -> list[tuple[int, int, int]]:
    s, w, n, e = bounds
    x0, y0 = tile_xy(n, w, z)
    x1, y1 = tile_xy(s, e, z)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def plan_tiles(points: list[tuple[float, float]], budget: int, pad_m: float = 250,
               max_zoom: int = 12) -> tuple[list, int, int, tuple]:
    """Pick zoom levels so the whole area fits in `budget` tiles; deepest zoom first, then add coarser ones."""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    dlat = math.degrees(pad_m / EARTH_R)
    dlng = math.degrees(pad_m / (EARTH_R * math.cos(math.radians(sum(lats) / len(lats)))))
    bounds = (min(lats) - dlat, min(lngs) - dlng, max(lats) + dlat, max(lngs) + dlng)
    # the map box is ~3:1 on a desktop: widen the covered area so its sides aren't blank grey
    s_, w_, n_, e_ = bounds
    mid = (s_ + n_) / 2
    h_m = (n_ - s_) * 111320
    w_m = (e_ - w_) * 111320 * math.cos(math.radians(mid))
    if w_m < 2.8 * h_m:
        extra = (2.8 * h_m - w_m) / 2 / (111320 * math.cos(math.radians(mid)))
        bounds = (s_, w_ - extra, n_, e_ + extra)
    zmax = None
    # OpenStreetMap's tile policy forbids bulk prefetching of detailed zooms: stop at 12 by default
    for z in range(min(max_zoom, 17), 8, -1):
        if len(tiles_for(bounds, z)) <= budget * 0.7:
            zmax = z
            break
    if zmax is None:
        return [], 0, 0, bounds
    chosen, zmin = tiles_for(bounds, zmax), zmax
    for z in range(zmax - 1, 8, -1):
        t = tiles_for(bounds, z)
        if len(chosen) + len(t) > budget:
            break
        chosen, zmin = chosen + t, z
    return chosen, zmin, zmax, bounds


def hide_private(c: dict | None, private: set[str]) -> dict | None:
    """Places marked `private: true` (someone's home) stay out of the shareable page: no name, no point."""
    if not c or not private:
        return c
    c = json.loads(json.dumps(c))
    c["places_min"] = {k: v for k, v in (c.get("places_min") or {}).items() if k not in private}
    if (c.get("access") or {}).get("to") in private:
        c["access"]["to"] = None
    return c


# ---- data assembly ------------------------------------------------------------------------------
def assemble(cfg: dict, data_dir: Path) -> dict:
    search = {r["id"]: r for r in json.loads((data_dir / "01_search.json").read_text())}
    fins = json.loads((data_dir / "04_finalists.json").read_text())["finalists"]
    photos = json.loads((data_dir / "05_photos.json").read_text())
    texts_path = data_dir / "06_texts.json"
    texts = json.loads(texts_path.read_text()) if texts_path.exists() else {"overview": "", "listings": []}
    by_id = {t["id"]: t for t in texts.get("listings", [])}
    ctx_path = data_dir / "01b_context.json"
    ctx = json.loads(ctx_path.read_text()) if ctx_path.exists() else {}
    private = {p["name"] for p in (cfg.get("places") or []) + (cfg.get("anchors") or []) if p.get("private")}
    listings = []
    for f in fins:
        lid = f["id"]
        s = search.get(lid, {})
        d = json.loads((data_dir / "03_details" / f"{lid}.json").read_text())
        t = by_id.get(lid, {})
        listings.append({
            "id": lid, "rank": f["rank"], "title": s.get("title"), "url": s.get("url") or f"https://www.airbnb.com/rooms/{lid}",
            "headline": t.get("headline") or s.get("title"), "summary": f.get("summary"),
            "pros": t.get("pros", []), "cons": t.get("cons", []), "score": f.get("score"),
            "red_flags": f.get("red_flags", []),
            "price_total": s.get("price_total"), "price_per_night": s.get("price_per_night"),
            "currency": s.get("currency") or cfg["price"].get("currency"),
            "lat": s.get("lat"), "lng": s.get("lng"), "distances_m": {k: v for k, v in (s.get("distances_m") or {}).items() if k not in private},
            "near_boundary": s.get("near_boundary"),
            "rating": d.get("rating") or s.get("rating"), "reviews_count": d.get("reviews_count") or s.get("reviews_count"),
            "badges": s.get("badges", []), "free_cancellation": s.get("free_cancellation"),
            "property_type": d.get("property_type"), "capacity": d.get("capacity"), "overview": [o for o in d.get("overview", []) if not re.search(r"guest", o, re.I)],  # guests shown elsewhere
            "sub_ratings": d.get("sub_ratings", {}), "amenities": d.get("amenities", []),
            "house_rules": d.get("house_rules", []), "safety_and_property": d.get("safety_and_property", []),
            "host": d.get("host", {}), "pets_allowed": d.get("pets_allowed"), "cancellation_policy": d.get("cancellation_policy"),
            "reviews_meta": d.get("reviews_meta", {}),
            "criteria": mark_unconfirmed(f.get("criteria", {})),
            "context": hide_private(ctx.get(lid), private),
            "photos": [{**p, "caption": caption_text(p.get("caption"))} for p in photos.get(lid, [])],
        })
    names = list(listings[0]["criteria"].keys()) if listings else []
    labels = texts.get("criteria_labels") or {}
    ci, co = str(cfg["dates"]["checkin"]), str(cfg["dates"]["checkout"])
    return {
        "trip": {"checkin": ci, "checkout": co, "nights": (date.fromisoformat(co) - date.fromisoformat(ci)).days,
                 "guests": cfg["guests"], "currency": cfg["price"].get("currency"),
                 "max_total": cfg["price"].get("max_total")},
        "anchors": [a for a in cfg.get("anchors") or [] if not a.get("private")],
        "places": [p for p in cfg.get("places") or [] if not p.get("private")],
        "criteria": [{"key": n, "label": labels.get(n) or n.replace("_", " ")} for n in names],
        "overview": texts.get("overview", ""),
        "listings": listings,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ---- bundle -------------------------------------------------------------------------------------
def bundle(cfg: dict, data: dict, out_dir: Path, f: Fetcher) -> dict:
    per = int(cfg.get("bundle", {}).get("photos_per_listing", 12))
    files = []
    for L in data["listings"]:
        kept = []
        for i, p in enumerate(L["photos"][:per]):
            rel = f"photos/{L['id']}/{i:02d}.jpg"
            if f.binary(p["url"].split("?")[0] + "?im_w=720", out_dir / rel, delay=(0.2, 0.6)):
                kept.append({**p, "src": rel, "remote": p["url"]})
                files.append(rel)
        L["photos_total"] = len(L["photos"])
        L["photos"] = kept
    pts = [(L["lat"], L["lng"]) for L in data["listings"] if L["lat"] is not None]
    pts += [(p["lat"], p["lng"]) for p in data.get("places") or []]
    if data["anchors"]:  # cover each anchor's whole search circle, it is drawn on the map
        b = bbox_around_anchors([{**a, "max_distance_m": a.get("max_distance_m") or 0} for a in data["anchors"]], 0)
        pts += [(b.ne_lat, b.ne_lng), (b.sw_lat, b.sw_lng)]
    budget = int(cfg.get("bundle", {}).get("map_tiles_max", 120))
    max_zoom = int(cfg.get("bundle", {}).get("max_zoom", 12))
    tiles, zmin, zmax, bounds = plan_tiles(pts, budget, max_zoom=max_zoom) if pts else ([], 0, 0, None)
    ok = 0
    for z, x, y in tiles:
        rel = f"tiles/{z}/{x}/{y}.png"
        if f.binary(OSM.format(z=z, x=x, y=y), out_dir / rel, delay=(0.3, 0.8)):
            files.append(rel)
            ok += 1
    data["map"] = {"zmin": zmin, "zmax": zmax, "bounds": bounds, "tiles": ok}
    log.info("bundle: %d photos, %d/%d map tiles (zoom %s–%s)", sum(len(L["photos"]) for L in data["listings"]),
             ok, len(tiles), zmin, zmax)
    return {"files": files}


UI_DIR = ROOT / "templates" / "ui"
UI_SCHEMA = {"type": "object", "required": ["items"], "additionalProperties": False,
             "properties": {"items": {"type": "object", "additionalProperties": {"type": "string"}}}}


def ui_strings(lang: str, cfg: dict, data_dir: Path) -> dict[str, str]:
    """Page labels: shipped for en/ru; any other language is translated from English once and cached."""
    shipped = UI_DIR / f"{lang}.json"
    if shipped.exists():
        return json.loads(shipped.read_text())
    en = json.loads((UI_DIR / "en.json").read_text())
    cache = data_dir / f"ui_{lang}.json"
    if cache.exists():
        got = json.loads(cache.read_text())
        if set(en) <= set(got):
            return got
    prompt = (f"Translate the labels of a web page for choosing a place to stay into {language_name(lang)}. "
              "Keep every {placeholder} in braces exactly as is, keep symbols like ◆ • ● ↗ ★ and quotes around "
              "{name} natural for the language. `months` is a comma-separated list of the 12 month names in the form "
              "used after a day number (\"19 October\"); keep it comma-separated. Answer with `items`: "
              "the same keys, translated values.\n\n```json\n" + json.dumps(en, ensure_ascii=False, indent=1) + "\n```")
    res = call(prompt, UI_SCHEMA, model=(cfg.get("models") or {}).get("translate", "sonnet"),
               log_dir=data_dir / "model_logs", name=f"05_ui_{lang}")
    got = {**en, **{k: v for k, v in res["items"].items() if k in en and v.strip()}}
    atomic_write(cache, json.dumps(got, ensure_ascii=False, indent=1))
    return got


def page_title(checkin: str, checkout: str, ui: dict) -> str:
    """'Stays 10–15 November' / 'Stays 28 October – 2 November', in the page language."""
    months = [m.strip() for m in ui.get("months", "").split(",")]
    if len(months) != 12:
        months = [str(i) for i in range(1, 13)]
    a, b = date.fromisoformat(checkin), date.fromisoformat(checkout)
    if a.month == b.month:
        rng = f"{a.day}–{b.day} {months[b.month - 1]}"
    else:
        rng = f"{a.day} {months[a.month - 1]} – {b.day} {months[b.month - 1]}"
    return ui.get("title", "{range}").replace("{range}", rng)


LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"
FONTS_RE = re.compile(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com/[^"]+)">')


def self_hosted(page: str, out_dir: Path, f: Fetcher, lang: str) -> str:
    """index.html for offline use or any static host: fonts and Leaflet next to the page, nothing loaded from other hosts."""
    assets = out_dir / "assets"
    (assets / "fonts").mkdir(parents=True, exist_ok=True)
    m = FONTS_RE.search(page)
    css = None
    if m:
        url = m.group(1).replace("&amp;", "&")
        try:  # chrome UA → woff2 + unicode-range subsets
            css = f.page(url, "google_fonts_css_" + hashlib.sha1(url.encode()).hexdigest()[:10], re.compile("@font-face"),
                         delay=(0.1, 0.3))
        except Exception as e:  # no network / blocked: the page falls back to system fonts
            log.warning("web fonts unavailable (%s), the page uses system fonts", e)
            page = page.replace(m.group(0), "")
    if m and css:
        def local(u):
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", u.split("/s/", 1)[-1])
            ok = f.binary(u, assets / "fonts" / name, delay=(0.1, 0.3))
            return f"url(assets/fonts/{name})" if ok else f"url({u})"
        css = re.sub(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", lambda mm: local(mm.group(1)), css)
        page = page.replace(m.group(0), f"<style>\n{css}\n</style>")
    page = re.sub(r'<link rel="preconnect"[^>]*>\n?', "", page)
    atomic_write(assets / "leaflet.min.js", (VENDOR / "leaflet.min.js").read_text())
    (assets / "LICENSE-leaflet.txt").write_text((VENDOR / "LICENSE").read_text())
    page = page.replace(f'<script src="{LEAFLET_JS}"></script>', '<script src="assets/leaflet.min.js"></script>')
    return (f'<!doctype html>\n<html lang="{lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<meta name="robots" content="noindex, nofollow"></head><body>\n' + page + "\n</body></html>\n")


def script_json(o) -> str:
    """JSON safe inside <script>: no '<' at all (covers </script> and the <!--<script> trap), no U+2028/9."""
    return (json.dumps(o, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def fill(tpl: str, title: str, data: dict, ui: dict, f: Fetcher) -> str:
    js = lambda o: script_json(o)
    return (tpl.replace("__TITLE__", title.replace("<", "&lt;"))
               .replace("/*__LEAFLET_CSS__*/", leaflet_css(f))
               .replace("/*__UI__*/null", js(ui))
               .replace("/*__DATA__*/null", js(data)))


def artifact_page(tpl: str, title: str, data: dict, ui: dict, out_dir: Path, cfg: dict, f: Fetcher) -> str:
    """Compact page for claude.ai (it refuses public sharing of versions with hundreds of files):
    fewer photos and coarser map, same content. Writes manifest-artifact.json with its file list."""
    ab = cfg.get("artifact_bundle") or {"photos_per_listing": 5, "max_zoom": 12}
    d = json.loads(json.dumps(data))
    files = []
    for L in d["listings"]:
        L["photos"] = L["photos"][: int(ab.get("photos_per_listing", 5))]
        files += [p["src"] for p in L["photos"]]
    m = d.get("map") or {}
    if m.get("zmax"):
        cap = min(int(ab.get("max_zoom", 12)), m["zmax"])
        m["zmax"] = cap
        files += sorted(p.relative_to(out_dir).as_posix() for p in (out_dir / "tiles").glob("*/*/*.png")
                        if int(p.parts[-3]) <= cap and m.get("zmin", 0) <= int(p.parts[-3]))
    atomic_write((out_dir / "manifest-artifact.json"), json.dumps({"page": "artifact.html", "files": files}, indent=1))
    return fill(tpl, title, d, ui, f)


def leaflet_css(f: Fetcher) -> str:
    return (VENDOR / "leaflet.min.css").read_text()


def pack(out_dir: Path, files: list[str], name: str) -> Path:
    """One zip of the offline page: index.html + assets + photos + tiles."""
    dest = out_dir / f"{name}-page.zip"
    paths = ["index.html"] + files + [p.relative_to(out_dir).as_posix() for p in (out_dir / "assets").rglob("*") if p.is_file()]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in dict.fromkeys(paths):
            z.write(out_dir / rel, f"{name}/{rel}")
    return dest


def run(cfg: dict, data_dir: Path, out_dir: Path, f: Fetcher) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = assemble(cfg, data_dir)
    files = bundle(cfg, data, out_dir, f)["files"]
    data = localize(data, cfg, data_dir, f)  # degrades by itself: no rate → source currency, no model → as is
    lang = data["lang"]
    try:
        ui = ui_strings(lang, cfg, data_dir)
    except ModelError as e:
        log.warning("page labels not translated to %s (%s): using English", lang, e)
        ui = json.loads((UI_DIR / "en.json").read_text())
    title = (cfg.get("display") or {}).get("title") or page_title(data["trip"]["checkin"], data["trip"]["checkout"], ui)
    tpl = TEMPLATE.read_text()
    atomic_write((out_dir / "artifact.html"), artifact_page(tpl, title, data, ui, out_dir, cfg, f))
    atomic_write((out_dir / "index.html"), self_hosted(fill(tpl, title, data, ui, f), out_dir, f, lang))
    atomic_write((out_dir / "manifest.json"), json.dumps({"page": "index.html", "files": files}, indent=1))
    trip_name = re.sub(r"[^A-Za-z0-9_-]+", "-", out_dir.resolve().parent.name).strip("-") or "trip"
    z = pack(out_dir, files, trip_name)
    log.info("page: %s (%d supporting files), zip %s", out_dir / "index.html", len(files), z)
    return {"page (open in a browser)": out_dir / "index.html", "zip (to send)": z,
            "claude.ai artifact": out_dir / "artifact.html", "its files": out_dir / "manifest-artifact.json"}
