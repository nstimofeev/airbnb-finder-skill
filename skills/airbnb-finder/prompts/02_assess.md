You check one Airbnb listing against the trip criteria. The listing's data is below:

- `search` — price for the whole stay, rating, badges, beds.
- `surroundings` — computed by code from OpenStreetMap and a router (free-flow, no traffic):
  - `food` — places to eat: how many restaurants within 10/15/20 minutes by car, and the nearest by name;
  - `nature` — viewpoints, peaks, hiking routes with driving time;
  - `places_min` — driving minutes to the trip's key places;
  - `neighbours` — OSM buildings within 150/300 m and other Airbnb listings within 300 m (a sign of a village);
  - `cluster` — Airbnb listings within 400 m on any dates (including ones booked on ours) and
    `same_brand_nearby` — nearby listings with a similar title, almost certainly the same host (a cabin complex);
  - `access` — the first 5 km of road from the house: metres of `paved`, `unpaved` and `unknown` (no surface
    in OSM); `snap_to_road_m` — how far the listing point is from the nearest road.
  Limits: Airbnb fuzzes listing coordinates by ~150 m; OSM is incomplete in rural areas (restaurants and road
  surfaces may be missing) — absent from the data is not absent in reality; look for confirmation in the
  description and reviews.
- `details` — description, amenities, sub-ratings, house rules, host, cancellation policy, `pets_allowed`.
- `reviews` — NOT all reviews but an extract: `snippets` — sentences from reviews that mention a criterion
  (keyword search, worst and newest first); `low_rated_reviews` — full reviews rated 3 or lower;
  `stats` — how many reviews were collected and the rating distribution. No mentions is no evidence
  either way.

For each criterion:
- `verdict`: `ok` — confirmed and no serious complaints; `warn` — no data or isolated complaints;
  `fail` — directly contradicted (amenity missing, a rule forbids it, repeated complaints, bad surroundings numbers).
- `note`: 1–2 sentences in {{LANGUAGE}} on what the verdict rests on (surroundings numbers, an amenity, how
  many reviews and from which years).
- `quotes_for` / `quotes_against`: up to 3 short quotes from reviews or the description (a fragment, not the
  whole review). Copy them character for character in the original language — don't translate parts, don't
  mix languages, don't shorten words; code later checks quotes against the review text. If a quote is not in
  English or {{LANGUAGE}}, add a translation into {{LANGUAGE}} in square brackets after it. Start with the
  year: "(2024) …". Never invent quotes. Surroundings numbers are not quotes — they go into `note`.
- The verdict must match the note: if your `note` names a failure sign from the criterion's description
  (e.g. "neighbouring cabins of the same host", "pets allowed", "a guest had a dog") — the verdict is `fail`,
  not `ok` or `warn`. Good reviews don't cancel such a sign.
- Recent reviews (last 2 years) weigh more than old ones; a host reply that admits and fixes a problem is a plus.

`summary` — 2–3 sentences in {{LANGUAGE}}: the main pros and cons.
`score` — 0–100, overall fit for these criteria, weighted by their priority.
`red_flags` — serious problems outside the criteria (photos not matching, host cancellations, safety,
hidden fees), in {{LANGUAGE}}; an empty list if none.

## Trip criteria

{{CRITERIA}}
