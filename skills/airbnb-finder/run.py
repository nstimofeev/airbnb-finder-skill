"""Airbnb Finder: find a place to stay on Airbnb by what search filters can't express.

  python run.py init  trips/lisbon            # new trip folder from the examples
  python run.py geocode "Sintra, Portugal"    # coordinates of a place, for config.yaml
  python run.py --trip trips/lisbon            # all steps; each skips what is already collected
  python run.py --trip trips/lisbon search     # one step: search | context | narrow | details | select | photos | page
  python run.py --trip trips/lisbon page --force   # redo a step

Works on Windows, macOS and Linux with Python 3.10+. On first start it creates its own virtual environment
in ~/.airbnb-finder/venv (override: AIRBNB_FINDER_HOME) and installs the dependencies there.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AIRBNB_FINDER_HOME") or Path.home() / ".airbnb-finder")
VENV = HOME / "venv"
STEPS = ["search", "context", "narrow", "details", "select", "photos", "page"]


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def bootstrap() -> None:
    """Re-run this script inside our own venv, in UTF-8 mode (Windows defaults to a legacy code page)."""
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ is required, this is {sys.version.split()[0]}")
    in_venv = Path(sys.prefix).resolve() == VENV.resolve()
    if in_venv and sys.flags.utf8_mode:
        return
    if not in_venv:
        req = ROOT / "requirements.txt"
        want = hashlib.sha1(req.read_bytes()).hexdigest()
        stamp = VENV / ".requirements.sha1"
        if not venv_python().exists():
            import venv
            print(f"First run: creating a virtual environment in {VENV} …", file=sys.stderr)
            try:
                venv.create(VENV, with_pip=True)
            except Exception as e:  # e.g. Debian/Ubuntu without python3-venv
                sys.exit(f"Could not create a virtual environment ({e}). On Debian/Ubuntu: install python3-venv.")
        if not stamp.exists() or stamp.read_text().strip() != want:
            print("Installing dependencies …", file=sys.stderr)
            subprocess.run([str(venv_python()), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "-r", str(req)], check=True)
            stamp.write_text(want)
    # subprocess instead of exec: os.execv on Windows detaches from the console
    sys.exit(subprocess.run([str(venv_python()), "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]]).returncode)


if __name__ == "__main__" and not os.environ.get("AIRBNB_FINDER_NO_BOOTSTRAP"):
    bootstrap()

import argparse  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402

sys.path.insert(0, str(ROOT))
log = logging.getLogger("run")

DERIVED = ["01_search.json", "01_search_all.json", "01b_context.json", "02_shortlist.json", "03_details",
           "03_assess", "04_finalists.json", "05_photos.json", "06_texts.json", "model_logs"]


def archive_if_trip_changed(cfg: dict, data: Path) -> None:
    """Step outputs are skipped when present, so a date/guest change must move the old ones away.
    Raw responses stay in data/raw (their cache keys already include dates and guests)."""
    from src.step1_search import trip_key
    stamp = data / "trip_key.txt"
    key = trip_key(cfg)
    old = stamp.read_text().strip() if stamp.exists() else None
    if old and old != key:
        dest = data / f"archive-{old}"
        dest.mkdir(exist_ok=True)
        moved = [n for n in DERIVED if (data / n).exists()]
        for n in moved:
            (data / n).rename(dest / n)
        log.warning("dates/guests changed (%s → %s): moved previous results to %s", old, key, dest)
    stamp.write_text(key)


def load_config(path: Path) -> dict:
    import yaml
    cfg = yaml.safe_load(path.read_text())
    # YAML turns 2026-11-10 into a date object; everything downstream wants ISO strings
    cfg["dates"] = {k: str(v) for k, v in (cfg.get("dates") or {}).items()}
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    errs = []
    for k in ("checkin", "checkout"):
        v = str(cfg.get("dates", {}).get(k, ""))
        if not v or "YYYY" in v:
            errs.append(f"dates.{k} is not set")
    for a in cfg.get("anchors") or []:
        if a.get("name") in (None, "", "...") or not a.get("max_distance_m"):
            errs.append(f"anchor {a.get('name')!r}: name, lat, lng and max_distance_m are required")
    zone = cfg.get("zone") or {}
    has_zone = any((zone.get("ne") or {}).values()) and any((zone.get("sw") or {}).values())
    if not has_zone and not [a for a in cfg.get("anchors") or [] if a.get("max_distance_m")]:
        errs.append("either zone (ne/sw) or at least one anchor is required")
    if cfg.get("geometry_mode", "all") not in ("all", "any"):
        errs.append("geometry_mode: all or any")
    if errs:
        raise SystemExit("config.yaml:\n  - " + "\n  - ".join(errs))


def init_trip(trip: Path) -> None:
    trip.mkdir(parents=True, exist_ok=True)
    for src, dst in (("config.example.yaml", "config.yaml"), ("criteria.example.md", "criteria.md")):
        if (trip / dst).exists():
            print(f"{trip / dst} exists, left as is")
        else:
            shutil.copyfile(ROOT / "examples" / src, trip / dst)
            print(f"created {trip / dst}")


def geocode(query: str) -> None:
    """OpenStreetMap Nominatim, free and keyless; its rules: at most 1 request per second, a real User-Agent."""
    import json
    import urllib.parse
    import urllib.request
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": query, "format": "json", "limit": 5})
    req = urllib.request.Request(url, headers={"User-Agent": "airbnb-finder-skill/1.0 (trip planning)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        hits = json.loads(r.read().decode("utf-8"))
    print(json.dumps([{"name": h.get("display_name"), "lat": round(float(h["lat"]), 5), "lng": round(float(h["lon"]), 5),
                       "type": f"{h.get('class')}/{h.get('type')}"} for h in hits], ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=STEPS + ["all", "init", "geocode"], default="all")
    ap.add_argument("arg", nargs="?", help="init: trip folder; geocode: place name")
    ap.add_argument("--trip", help="trip folder with config.yaml + criteria.md; data/ and output/ go inside it")
    ap.add_argument("--force", action="store_true", help="redo the step even if its output exists")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.step == "init":
        if not (a.arg or a.trip):
            ap.error("init needs a folder: run.py init trips/<name>")
        init_trip(Path(a.arg or a.trip))
        return 0
    if a.step == "geocode":
        if not a.arg:
            ap.error('geocode needs a place: run.py geocode "Sintra, Portugal"')
        geocode(a.arg)
        return 0
    if not a.trip:
        ap.error("--trip <folder> is required (create one with: run.py init trips/<name>)")

    from src import model_steps, render, step1_search, step1b_context, step2_details, step3_photos
    from src.fetch import BlockedError, Fetcher
    from src.model import ModelError

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    trip = Path(a.trip)
    cfg = load_config(trip / "config.yaml")
    data, out, prompts = trip / "data", trip / "output", ROOT / "prompts"
    model_steps.CRITERIA_FILE = trip / "criteria.md"
    model_steps.LANGUAGE = ((cfg.get("display") or {}).get("language") or "en").lower()
    data.mkdir(parents=True, exist_ok=True)
    archive_if_trip_changed(cfg, data)
    net = {**(cfg.get("network") or {}), "currency": cfg["price"].get("currency", "USD")}
    f = Fetcher(data / "raw", net)
    steps = STEPS if a.step == "all" else [a.step]
    try:
        for s in steps:
            log.info("=== %s ===", s)
            if s == "search":
                step1_search.run(cfg, data, f, a.force)
            elif s == "context":
                step1b_context.run(cfg, data, f, a.force)
            elif s == "narrow":
                model_steps.narrow(cfg, data, prompts, a.force)
            elif s == "details":
                step2_details.run(cfg, data, f, a.force)
            elif s == "select":
                model_steps.assess(cfg, data, prompts, a.force)
                model_steps.rank(cfg, data, prompts, a.force)
            elif s == "photos":
                step3_photos.run(cfg, data, f, a.force)
            elif s == "page":
                model_steps.texts(cfg, data, prompts, a.force)
                res = render.run(cfg, data, out, f)
                print("\nDone:")
                for k, v in res.items():
                    print(f"  {k}: {v}")
    except (BlockedError, ModelError) as e:
        log.error("%s", e)
        return 2
    except FileNotFoundError as e:
        log.error("missing input file: %s — run the previous step first", e.filename)
        return 2
    log.info("network requests this run: %d", f.network_requests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
