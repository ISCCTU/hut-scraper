---
name: hut-search
description: Search and rank Czech cottage/hut rentals for a group trip using this repo's scraper pipeline (data/filtered.json, cli.py). Asks for trip dates, group size, budget, location/transit, amenities, rating and availability preferences, then returns a ranked shortlist with per-site data-quality caveats. Use this whenever the user asks for cottage/chalupa/chata candidates, a venue shortlist, or to "find a place" for the trip - not just when they say "run the skill".
---

# Hut search

This repo scrapes four Czech cottage-rental sites into `data/filtered.json`, a
ranked list already scored against the trip requirements in
`pipeline/config.py`. Your job is to turn that into a shortlist tailored to
what's actually being asked right now, rather than just dumping the top rows
of the file - several useful filters (minimum rating, availability
tolerance, sort order) only exist as an ad hoc pass over the data today, not
as real `pipeline/filter.py` options, so you apply them yourself each time.

## 1. Ask for trip parameters

Use `AskUserQuestion`, batched across a couple of calls (max 4 questions and
4 options per call). Default every question to the current value in
`pipeline/config.py` so the user can breeze through on defaults instead of
re-specifying a whole trip from scratch - read the file before asking so the
defaults you show are real, not remembered from a stale prior run.

Ask about:
- **Trip dates** - default `TRIP_CHECKIN`/`TRIP_CHECKOUT`.
- **Group size** - min acceptable (`MIN_CAPACITY`) and preferred (`PREFERRED_CAPACITY`).
- **Budget ceiling** - default `BUDGET_MAX_CZK`.
- **Origin + max transit time/distance** - default `ORIGIN_NAME`, `MAX_PRAGUE_DISTANCE_KM`.
- **Must-have amenities** (multi-select) - common room, sauna/wellness, pool, fireplace, whirlpool, single-building-only.
- **Minimum rating / minimum review count**, or "no preference" - don't skip this one. Rating/review-count data exists in the file (`rating_score`, `rating_scale`, `rating_count`) but nothing filters or sorts on it unless you do.
- **Availability tolerance** - exclude listings with unknown availability entirely, or include them flagged as unconfirmed. cs-chalupy.cz publishes a real per-day calendar (`available_for_trip`/`availability_note`), so "unknown" here is a meaningful, non-trivial fraction of that site's listings, not a rounding error.
- **Sort/rank preference** - best overall score (the file's own `_score`), cheapest first, highest-rated first, or most-reviewed first.

Don't add a "max candidates per source" / diversity-cap question - it was
considered for this skill and deliberately left out, so a user who wants
that has to ask for it explicitly rather than being prompted every time.

If the user's request already answers some of these in plain language
("20 candidates, budget 20k, must have a sauna"), don't re-ask what they
already told you - fill those in and only ask about what's genuinely open.

## 2. Decide whether to re-run the pipeline

Default to the fast path: read `data/filtered.json` as-is (it's already
geocoded, transit-timed, and scored). Only run
`uv run python cli.py filter` first if `data/combined.json` is newer than
`data/filtered.json`, or `data/filtered.json` doesn't exist yet. Only run
the full `uv run python cli.py all` (slow - scrapes all four sites live) if
the user says the data is stale or explicitly asks for a fresh scrape.
Never trigger a live scrape on your own judgment that the data "might be
old" - ask first, the same way any other slow network operation should be
confirmed rather than assumed.

## 3. Apply the extra filters yourself

`data/filtered.json` is already filtered/scored for capacity, budget,
availability-not-False, and the standing config knobs. What it does *not*
do natively: amenity matching, a minimum rating/review-count floor,
excluding (vs. flagging) unknown-availability listings, or any sort order
other than the file's own `_score`. Apply these as a plain Python pass over
the loaded JSON - load it, filter, sort, slice to the requested count - the
same way this was done by hand across the conversation this skill was built
from. Say so plainly when you present results ("rating/availability
filters applied on top of the pipeline's own output") rather than implying
`pipeline/filter.py` has first-class options for them - if the user wants
that promoted into the real pipeline later, treat it as a separate,
explicit follow-up, not something to silently fold in here.

## 4. Output

A ranked markdown table: hut name (linked), source, capacity, price
estimate (`_price_estimate_low_czk`-`_price_estimate_high_czk` with
`_display_price_note`), transit time, rating (`rating_score`/`rating_scale`
+ `rating_count`, or "unrated" - never blank), availability status, and
which requested amenities matched. Render `None`/unknown values as
"unconfirmed", never as a blank cell or as a negative signal - a listing
the scrapers never resolved a transit time for is not worse than one that's
confirmed far away, it's just unmeasured.

Follow the table with two short footers:
- **Data-quality caveats** - pull the relevant bullets from this repo's
  `README.md` "Known limitations" section rather than re-deriving your own
  summary of per-site gaps; that section is kept current and is the source
  of truth.
- **Logistics reminder** - a short checklist of things that matter for a
  group trip but aren't scraped data at all, so they can't be filtered on:
  minimum-night-stay requirements that could conflict with a short weekend,
  bathroom/toilet count versus group size, parking for a group arriving
  together, kitchen size for self-catering a group that size, whether
  final cleaning/linens/a deposit are included in the quoted price, and any
  noise/party/quiet-hours policy if the trip is a social event rather than
  a quiet retreat. Frame this explicitly as "verify these yourself per
  finalist" - the scrapers have no way to know any of it.

## Non-goals

- Never silently re-scrape (live network, slow) without asking first - see
  step 2.
- Never invent a value a scraper doesn't have. If `bedrooms`, a bathroom
  count, or anything else wasn't captured, say it's unknown rather than
  guessing from capacity or price.
- Never drop an unknown/`None` value silently - always render it as
  "unconfirmed" so the user can tell "checked and it's bad" apart from
  "never checked."
