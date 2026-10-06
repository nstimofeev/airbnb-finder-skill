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
HOME = Path(os.environ.get("AIRBNB_FINDER_HOME") or Path.home() / ".airbnb-finder").expanduser().resolve()
VENV = HOME / "venv"
STEPS = ["search", "context", "narrow", "details", "select", "photos", "page"]


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_healthy() -> bool:
    """The venv's own python starts and imports our packages (a removed/upgraded base Python breaks it)."""
    try:
        return subprocess.run([str(venv_python()), "-c", "import curl_cffi, yaml, jsonschema"],
                              capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def bootstrap() -> None:
    """Re-run this script inside our own venv, in UTF-8 mode (Windows defaults to a legacy code page)."""
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ is required, this is {sys.version.split()[0]} ({sys.executable}). "
                 "Install a newer Python (python.org, Homebrew, or the Microsoft Store) and run with it.")
    in_venv = Path(sys.prefix).resolve() == VENV.resolve()
    if in_venv and sys.flags.utf8_mode:
        return
    if not in_venv:
        req = ROOT / "requirements.txt"
        want = hashlib.sha256(req.read_bytes() + f"{sys.version_info[:2]} {sys.platform}".encode()).hexdigest()
        stamp = VENV / ".requirements.sha256"
        fresh = stamp.exists() and stamp.read_text().strip() == want
        if not (fresh and venv_python().exists() and venv_healthy()):
            import shutil
            import venv
            print(f"Setting up a virtual environment in {VENV} (one time) …", file=sys.stderr)
            try:
                venv.create(VENV, with_pip=True, clear=True)
                subprocess.run([str(venv_python()), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                "-r", str(req)], check=True)
            except Exception as e:  # e.g. Debian/Ubuntu without python3-venv, no network, 32-bit Python
                shutil.rmtree(VENV, ignore_errors=True)  # never leave a half-made venv behind
                sys.exit(f"Could not set up the virtual environment: {e}\n"
                         "On Debian/Ubuntu install python3-venv; you need 64-bit Python 3.10+ and internet access "
                         "to PyPI. Then run the same command again.")
            stamp.write_text(want)
    cmd = [str(venv_python()), "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]]
    if os.name != "nt":
        os.execv(cmd[0], cmd)  # same process: Ctrl+C reaches the pipeline directly
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the child gets Ctrl+C itself and stops cleanly
    sys.exit(subprocess.run(cmd).returncode)


NO_VENV = {"init"}  # stdlib only: works before the first install and offline
if (__name__ == "__main__" and not os.environ.get("AIRBNB_FINDER_NO_BOOTSTRAP")
        and not (len(sys.argv) > 1 and sys.argv[1] in NO_VENV)):
    bootstrap()

import argparse  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402

sys.path.insert(0, str(ROOT))
log = logging.getLogger("run")

DERIVED = ["01_search.json", "01_search_all.json", "01b_context.json", "02_shortlist.json", "03_details",
           "03_assess", "04_finalists.json", "05_photos.json", "06_texts.json", "model_logs"]


def archive_if_trip_changed(cfg: dict, data: Path) -> None:
    """Step outputs are skipped when present, so a date/guest/filter change must move the old ones away.
    Raw responses stay in data/raw (their cache keys already include dates, guests and filters)."""
    from src.step1_search import trip_key
    stamp = data / "trip_key.txt"
    key = trip_key(cfg)
    old = stamp.read_text().strip() if stamp.exists() else None
    if old and old != key:
        dest, n = data / f"archive-{old}", 2
        while dest.exists():  # back to earlier dates: never merge into or overwrite an older archive
            dest, n = data / f"archive-{old}-{n}", n + 1
        dest.mkdir()
        for name in DERIVED:
            if (data / name).exists():
                os.replace(data / name, dest / name)
        log.warning("dates/guests/filters changed (%s → %s): moved previous results to %s", old, key, dest)
    stamp.write_text(key)  # only after every move succeeded


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    """The trip's config over the example's defaults, so deleting a technical key never breaks a step."""
    import yaml
    if not path.exists():
        raise SystemExit(f"{path} not found — create the trip with: run.py init <folder>")
    defaults = yaml.safe_load((ROOT / "examples" / "config.example.yaml").read_text())
    for k in ("dates", "zone", "anchors", "places"):  # trip-specific: never taken from the example
        defaults.pop(k, None)
    try:
        user = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"{path}: not valid YAML: {e}")
    cfg = deep_merge(defaults, user)
    # YAML turns 2026-11-10 into a date object; everything downstream wants ISO strings
    cfg["dates"] = {k: str(v) for k, v in (cfg.get("dates") or {}).items()}
    disp = cfg.setdefault("display", {}) or {}
    disp["language"] = str(disp.get("language") or "en").replace("_", "-").lower()
    validate_config(cfg)
    return cfg


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_config(cfg: dict) -> None:
    from datetime import date
    errs = []
    d = {}
    for k in ("checkin", "checkout"):
        v = str((cfg.get("dates") or {}).get(k, ""))
        try:
            d[k] = date.fromisoformat(v)
        except ValueError:
            errs.append(f"dates.{k}: expected YYYY-MM-DD, got {v!r}")
    if len(d) == 2 and d["checkout"] <= d["checkin"]:
        errs.append("dates: checkout must be after checkin")
    if not _num((cfg.get("guests") or {}).get("adults")) or cfg["guests"]["adults"] < 1:
        errs.append("guests.adults: at least 1")
    if not (cfg.get("price") or {}).get("currency"):
        errs.append("price.currency is required (e.g. USD)")
    for a in cfg.get("anchors") or []:
        if a.get("name") in (None, "", "...") or not _num(a.get("lat")) or not _num(a.get("lng")) \
                or not _num(a.get("max_distance_m")) or a["max_distance_m"] <= 0:
            errs.append(f"anchor {a.get('name')!r}: name, lat, lng and max_distance_m (> 0) are required")
    for pl in cfg.get("places") or []:
        if not pl.get("name") or not _num(pl.get("lat")) or not _num(pl.get("lng")):
            errs.append(f"place {pl.get('name')!r}: name, lat and lng are required")
    zone = cfg.get("zone") or {}
    half = any(any((zone.get(c) or {}).values()) for c in ("ne", "sw"))
    full = all(any((zone.get(c) or {}).values()) for c in ("ne", "sw"))
    if half and not full:
        errs.append("zone: set both ne and sw corners (or neither)")
    if full and not (zone["ne"]["lat"] > zone["sw"]["lat"] and zone["ne"]["lng"] > zone["sw"]["lng"]):
        errs.append("zone: ne must be north-east of sw")
    if not full and not cfg.get("anchors"):
        errs.append("either zone (ne/sw) or at least one anchor is required")
    if cfg.get("geometry_mode", "all") not in ("all", "any"):
        errs.append("geometry_mode: all or any")
    if cfg.get("pets", "any") not in ("any", "prefer_none", "only_none"):
        errs.append("pets: any, prefer_none or only_none")
    for k in ("shortlist", "finalists"):
        if not _num((cfg.get("funnel") or {}).get(k)) or cfg["funnel"][k] < 1:
            errs.append(f"funnel.{k}: a positive number")
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
    from curl_cffi import requests  # brings its own CA bundle (python.org Python on macOS has none)
    from src.fetch import USER_AGENT
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": query, "format": "json", "limit": 5})
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"geocode: HTTP {r.status_code} from OpenStreetMap; try again in a minute")
    hits = r.json()
    if not hits:
        print("nothing found — try adding the region or country")
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
    from src.fetch import BlockedError, Fetcher, HTTPStatusError, NetworkError
    from src.model import ModelError

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    trip = Path(a.trip)
    cfg = load_config(trip / "config.yaml")
    data, out, prompts = trip / "data", trip / "output", ROOT / "prompts"
    model_steps.CRITERIA_FILE = trip / "criteria.md"
    model_steps.LANGUAGE = cfg["display"]["language"]
    if errs := model_steps.check_criteria(cfg, prompts):
        raise SystemExit("criteria:\n  - " + "\n  - ".join(errs))
    if a.force and a.step != "all":
        later = [x for x in STEPS[STEPS.index(a.step) + 1:] if x != "page"]
        if later:
            log.warning("only `%s` is redone; later steps (%s) keep their results until you rerun them with --force",
                        a.step, ", ".join(later))
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
                try:
                    model_steps.texts(cfg, data, prompts, a.force)
                except ModelError as e:  # the page works without the summary texts
                    log.warning("page texts failed (%s): building the page without them", e)
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
    except (NetworkError, HTTPStatusError) as e:
        log.error("network trouble in step `%s`: %s. Rerun the same command later — collected data is reused.", s, e)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted. Rerun the same command to continue — collected data is reused.")
        return 130
    log.info("network requests this run: %d", f.network_requests)
    return 0


if __name__ == "__main__":
    sys.exit(main())
