# Airbnb Finder — a Claude Code skill

Airbnb's filters know price, bedrooms and "pets allowed". They don't know whether the house is really
secluded or one of twelve identical cabins, whether guests complain about the road, the heating or noise at
night, or how many restaurants are within 15 minutes by car. This skill finds out — for every candidate — and
gives you a page to decide on together.

**What you get:** a page with the finalists on a map, a comparison table, a card per listing (photos,
surroundings, verdict per criterion with review quotes, house rules, cancellation) and a quote explorer.
It works offline from a folder, as a zip to send, or as a claude.ai artifact.

**How it works:** scripts collect, the model judges.
1. Map search over your area (split into smaller boxes when there are too many results), mechanical filter.
2. Surroundings from OpenStreetMap: places to eat, viewpoints and trails, buildings and other listings
   around (is it a village?), road surface of the first 5 km; driving times from the public OSRM router.
3. The model picks a shortlist.
4. For each shortlisted listing: the full card, house rules, cancellation policy and up to a few hundred
   reviews, worst-rated first. Code greps the reviews for keywords per criterion; the model reads only the
   matching sentences plus every low-rated review.
5. The model gives a verdict per criterion with quotes; code checks the quotes against the source text and
   removes listings that fail a must-have criterion; the model ranks the rest.
6. The page: texts, translation into your language, prices converted to your currency (ECB rate).

No API keys, no Airbnb account, nothing paid. Model steps run through your own Claude Code (`claude -p`).

## Install

Requirements: [Claude Code](https://claude.com/claude-code) and Python 3.10+ (Windows, macOS or Linux).
On Windows prefer the native Claude Code installer (`claude.exe`).

As a plugin:

```
/plugin marketplace add nstimofeev/airbnb-finder-skill
/plugin install airbnb-finder@airbnb-finder
```

Or copy `skills/airbnb-finder` into `~/.claude/skills/`.

Then just ask Claude: *"Find us an Airbnb near Sintra for 10–15 November, two adults, quiet, no pets
ever, walkable to restaurants."* It asks what's missing, writes the trip's criteria with you, runs the
funnel in the background and hands you the page.

The first run creates a virtual environment in `~/.airbnb-finder/venv` (override with
`AIRBNB_FINDER_HOME`) and installs `curl_cffi`, `PyYAML` and `jsonschema` there.

## Manual use

```
python run.py init airbnb-trips/sintra          # creates config.yaml + criteria.md from examples/
python run.py geocode "Sintra, Portugal"         # coordinates for config.yaml
python run.py --trip airbnb-trips/sintra         # everything; each step skips what is already collected
python run.py --trip airbnb-trips/sintra page --force   # rebuild only the page
```

(`py -3 run.py …` on Windows.) The settings are explained in
[`examples/config.example.yaml`](skills/airbnb-finder/examples/config.example.yaml) and the workflow in
[`SKILL.md`](skills/airbnb-finder/SKILL.md).

Tests: `AIRBNB_FINDER_NO_BOOTSTRAP=1 python -X utf8 -m pytest -q skills/airbnb-finder/tests`
(with `pytest`, `curl_cffi`, `PyYAML`, `jsonschema` installed).

## Please read

- **Airbnb's terms of service do not allow automated access.** This tool reads public pages the way a
  browser does, slowly (2–5 s between requests) and stops by itself when Airbnb starts refusing. It is meant
  for planning your own trip. Whether to use it is your decision and your responsibility.
- Airbnb changes its site; when its internal request IDs change, the tool tries to find the new ones by
  itself, but expect occasional breakage.
- OpenStreetMap (Overpass), OSRM and frankfurter.app are free community services with fair-use limits.
  Don't run many trips in parallel.
- Listing coordinates are fuzzed by Airbnb by ~150 m; OpenStreetMap is incomplete in the countryside.
  Treat the surroundings numbers as strong hints, not facts.

## License

MIT
