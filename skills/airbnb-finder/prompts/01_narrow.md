You help choose an Airbnb stay for a trip. Below are listings that already passed a mechanical filter
(geography, price, type). Available now: short fields from the search (price, rating, badges, beds,
distances to anchor points) and, when present, `surroundings` from OpenStreetMap: places to eat within
15/20 minutes by car, viewpoints and hiking routes within 25 minutes, minutes to the trip's key places,
buildings within 150 m and Airbnb listings within 300 m (a sign of a village or holiday settlement), and the
surface of the first 5 km of road (`paved`/`unpaved`/`unknown`, metres).
OSM is incomplete in rural areas and listing coordinates are fuzzed by ~150 m — treat these as hints, not
verdicts. Reviews and amenities come in the next step.

Task: pick the `shortlist_size` most promising listings (a few fewer is fine if the rest are clearly worse).

How to choose:
- Go by the trip criteria below, keeping in mind that these fields can barely verify them —
  judge how likely each listing is to meet them.
- A rating below 4.7 is a strong minus; guest_favorite/superhost badges are a plus.
- `near_boundary: true` means the listing is just outside the requested radius (Airbnb fuzzes coordinates by
  ~150 m) — don't drop it for that alone, but prefer listings inside the radius when otherwise equal.
- Price: cheaper is better when otherwise equal, but quality beats savings within the budget.
- Don't take a batch of near-identical listings from one host — keep the best 1–2.
- `same_brand_nearby` > 0 — listings with a similar title nearby (a complex of one host's cabins);
  `airbnb_400m_any_dates` — listings within 400 m on any dates (many = a tourist settlement).
  If the criteria ask for seclusion, don't take these.
- The pet rule is unknown at this step; if the criteria are against pets, skip listings whose title or
  badges say "pet friendly".

In `notes`, for each chosen id — one short phrase in English on why it passed.
Answer only with data matching the schema; ids are strings exactly as in the input.

## Trip criteria

{{CRITERIA}}
