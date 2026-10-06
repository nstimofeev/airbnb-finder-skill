---
name: airbnb-finder
description: Find a place to stay on Airbnb by criteria the site's filters can't express — quiet at night, really secluded, no pets ever (allergies), restaurants within 15 minutes by car, trails nearby, a decent road in — by reading reviews, house rules and the surroundings (OpenStreetMap) of every candidate, then building a shareable comparison page with photos, a map and review quotes. Use when someone wants help choosing an Airbnb for specific dates and has wishes beyond price and bedrooms.
---

# Airbnb Finder

A funnel: scripts collect data, the model narrows, checks and ranks, a script builds the page.
No API keys, no Airbnb account, no paid services. The model steps run through `claude -p` (the user's own
Claude Code), so nothing else needs configuring.

`<skill>` below is this skill's base directory (shown above when the skill loads). Run Python as `python3`
on macOS/Linux and `py -3` (or `python`) on Windows. Python 3.10+ is needed; the first run creates its own
virtual environment in `~/.airbnb-finder/venv` and installs three packages there — tell the user once.

## 1. Interview — short, then fill in sensible defaults

Ask only what you can't infer, in one batch:
- destination, dates, number of adults/children;
- budget for the whole stay (or none) and the currency to search in;
- what matters, in priority order, and what would rule a place out (deal-breakers);
- pets: fine / prefer none / never (allergy);
- page language and the currency to show prices in (default: the language you're talking in, and the search
  currency);
- how many finalists (default 8).

Mention once, briefly: this reads Airbnb's public pages the way a browser does, slowly and politely; it is
for personal use and Airbnb's terms don't permit automated access, so the user decides.

## 2. Create the trip folder

```
<python> <skill>/run.py init airbnb-trips/<short-name>
```

This copies `config.yaml` and `criteria.md` from `<skill>/examples/`. Put trips under the user's current
directory unless they say otherwise. Then edit both files:

**config.yaml** — dates, guests, price, `pets`, `display.language` / `display.currency`, `funnel.finalists`.
Where to look:
- a city or a neighbourhood: one or more `anchors` with `max_distance_m` (straight line);
- a region (national park, coast, valley): a `zone` box (ne/sw corners) plus `places` — the spots the trip
  is about (trailheads, beaches, towns). Driving minutes to each place are computed for every listing,
  and the first place is used to judge the road in, so put the nearest town/hub first.
Get coordinates with `<python> <skill>/run.py geocode "<place, country>"` (OpenStreetMap; one call per
second at most). Check that the box isn't absurdly large: every ~270 listings costs another round of search
pages, and Airbnb tolerates only a polite pace.

Other switches worth knowing (all documented in the example config):
- `pets: only_none` — listings that allow pets are removed by code, not just ranked lower;
- `hard_criteria` / `must_pass` — criterion keys whose `fail` removes a listing (by default: the first three);
- `village_filter` — keep only isolated houses (few buildings and other listings around);
- for a city trip set `context.poi_radius_km: 5` (surroundings are about food and neighbours, not trails).

**criteria.md** — one `### key` heading per criterion (lowercase latin, digits, `_`), in priority order,
each with a plain description: what counts as good, what counts as a failure (be concrete — "two or more
recent reviews about noise", "pets allowed", "neighbouring cabins of the same host"), and where evidence
lives (amenities, house rules, reviews, surroundings numbers). The model judges every listing against
exactly this text, so vague criteria give vague verdicts. Remove the example criteria. Show the user the
criteria and get a yes before the long run.

## 3. Run

```
<python> <skill>/run.py --trip airbnb-trips/<short-name>
```

Run it in the background and keep the log (`> run.log 2>&1`). It takes roughly 30–120 minutes, mostly
deliberate pauses between Airbnb requests. Steps, each skipped when its output already exists:

| step | what | output in `data/` |
|---|---|---|
| search | map search, split into smaller boxes when there are too many results; mechanical filter | `01_search.json` |
| context | OpenStreetMap: food, nature, buildings, road surface; driving times (OSRM) | `01b_context.json` |
| narrow | model picks a shortlist from short fields + surroundings | `02_shortlist.json` |
| details | listing card, house rules, cancellation, reviews (worst first) | `03_details/<id>.json` |
| select | keyword grep of reviews → model checks each listing per criterion → code drops hard fails → model ranks | `03_assess/`, `04_finalists.json` |
| photos | photo lists of the finalists | `05_photos.json` |
| page | texts, translation, prices, the page | `output/` |

Re-run one step with `<python> <skill>/run.py --trip <dir> <step> --force`. Changing only the page (language,
currency, title, how many photos) needs `page --force` alone — no new requests to Airbnb. After changing
`criteria.md`, re-run `select --force` and `page --force`; if the shortlist itself is wrong, `narrow --force`
first (with `funnel.extend_shortlist: true` it adds candidates instead of replacing them).

If the log says Airbnb refused (403/429) several times in a row, the run stops by itself: wait an hour and
start the same command again — everything already collected is reused. If a model step fails, its prompt
and raw answer are in `data/model_logs/`.

Things the user should know when reading results:
- listing coordinates are fuzzed by Airbnb by ~150 m; OpenStreetMap is incomplete in the countryside;
- review quotes are checked against the source text by code; ones that weren't found are marked
  "paraphrase";
- if few listings survive, say so plainly — loosening a criterion or widening the area is the user's call.

## 4. Deliver the page

The `page` step prints what it built in `output/`:
- `index.html` — the full page (photos, map, review quotes, comparison table); opens from disk in any
  browser, works offline, and can be uploaded as-is to any static web host;
- `<trip>-page.zip` — the same folder as one file, to send to someone (they unzip and open `index.html`);
- `artifact.html` + `manifest-artifact.json` — a smaller copy (fewer photos, coarser map) for claude.ai.

If you have the Artifact tool, publish `artifact.html` with `files` = every path listed in
`manifest-artifact.json`, mapped to the same path under `output/` (e.g. `"photos/123/00.jpg":
"<trip>/output/photos/123/00.jpg"`). The artifact is private; the user shares it from claude.ai. Public
sharing may refuse a page with very many files — then offer the zip.

Give the user, in a few lines: the top 2–3 with the main trade-off, anything that worried you (few
reviews, hard fails that removed most candidates), and where the page is.
