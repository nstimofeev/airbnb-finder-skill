Build keyword lists for searching Airbnb reviews, one list per trip criterion below.
Code matches a keyword at the start of a word in the review text (a stem: `restaur` finds "restaurant",
"restaurante", "restaurants"), so give word STEMS without endings, lowercase.

Reviews are written in many languages. For each criterion give 20–50 stems in English, in the local
language(s) of the destination (see the trip below; include every script the locals use) and in
{{LANGUAGE}}, plus a few of the most common ones in German, French, Spanish and Italian.

Include words for both the good and the bad side ("quiet" and "noisy", "asphalt" and "gravel", "warm" and
"cold"). Avoid stems so general that they appear in almost every review ("great", "nice", "good"):
they flood the search. At least 3 letters per stem.

Answer with the object `keywords`: criterion key → list of stems. Keys exactly as in the `### key` headings.

## Trip criteria

{{CRITERIA}}
