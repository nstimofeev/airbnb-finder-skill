You choose finalists from Airbnb listings that were already checked. For each listing below there is an
assessment per criterion (verdict + note), an overall score, red flags, price, rating, distances and short
surroundings numbers (`surroundings`: food within 15/20 min, trails, neighbours, road surface).

Task: choose and order as many finalists as requested (best — rank 1).
- Criteria are ordered by priority: a `fail` on the first criterion weighs more than on the last.
- Listings failing the trip's must-have criteria were already removed by code. Remaining `fail`s are a
  serious minus in the ranking (the higher the criterion's priority, the bigger), but not a ban.
- Red flags are a serious minus.
- Price matters when quality is close.
- Pets (`pets` in the parameters): `prefer_none` — when quality is close, rank higher the listing where pets
  are not allowed (`pets_allowed: false`); `null` means the rule isn't stated — neutral. `any` — ignore.
- Return exactly as many finalists as requested: every listing in the input already passed the must-have
  criteria. Fewer only if the input has fewer listings.

`summary` — 2–3 sentences in {{LANGUAGE}}: why the listing is in this place and how it is better or worse than
its neighbours in the list.

## Trip criteria

{{CRITERIA}}
