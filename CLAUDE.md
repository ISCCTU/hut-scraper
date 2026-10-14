# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

A one-off personal scraper for four Czech cottage/hut-rental sites, built to
find a place for a group weekend trip. Filters on capacity, budget, and
distance to the nearest bus/train stop. Standalone - unrelated to any other
project on this machine, has its own git history.

## Commands

```bash
uv sync                          # install deps into .venv
uv run python main.py            # core pipeline: 4 scrapers -> geocode -> filter -> export_report_data
uv run python -m scrapers.chatyachalupy   # run one scraper (also: e_chalupy, cs_chalupy, chata)
uv run python enrich_common_room.py  # patch chatyachalupy/e_chalupy raw JSON with has_common_room - BEFORE geocode.py
uv run python geocode.py         # fill missing lat/lon, find nearest stop, write data/combined.json
uv run python transit.py         # add prague_distance_km + IDOS transit time to combined.json - AFTER geocode.py
uv run python filter.py          # apply config.py's requirements, write data/filtered.json
uv run python export_report_data.py  # trim to report-data.json for the published report page
```

`main.py` does NOT run `enrich_common_room.py`/`transit.py` (slow, one request
per listing each) - see their own docstrings and README.md for the required
order (`enrich_common_room.py` before `geocode.py`, `transit.py` after it).
**Rerunning `geocode.py` rebuilds `data/combined.json` from `data/raw/*.json`
from scratch and silently drops anything `transit.py` added** - always rerun
`transit.py` again immediately after any `geocode.py` rerun.

`config.py` holds the trip requirements (dates, capacity, budget, origin
city for distance/transit-time) - edit it or override any value with an
identically-named env var. See its docstring and README.md's table; don't
hardcode a new date/capacity/budget/origin constant anywhere else.

## Architecture

`scrapers/common.py` defines the shared `Hut` dataclass every scraper
normalizes into, plus `build_session`/`polite_get`/`save_json` helpers. Each
site scraper is independent, writes `data/raw/<site>.json`, and can be
rerun on its own without touching the others. `geocode.py` merges all four
`data/raw/*.json` into `data/combined.json`, filling in `lat`/`lon` via
Nominatim where a site didn't provide them and finding the nearest
bus/train stop via the OSM Overpass API. `filter.py` reads
`data/combined.json`, drops anything under `MIN_CAPACITY` or confirmed
booked for the trip, scores the rest, and writes `data/filtered.json`.
`export_report_data.py` trims that down to what the published report page
actually renders.

Two optional enrichment passes sit outside that linear chain because of
where they need to run:
- `enrich_common_room.py` re-visits chatyachalupy.cz/e-chalupy.cz detail
  pages (their original scrapers' amenity keyword lists didn't include
  "společenská místnost" - cs-chalupy.cz/chata.cz already had it as a
  first-class checkbox) and patches `has_common_room`/`common_room_note`
  into `data/raw/*.json` **in place**. Must run before `geocode.py`, since
  it patches the raw files geocode.py reads from.
- `transit.py` adds `prague_distance_km` (cheap, straight-line, from
  existing lat/lon) and `transit_minutes_from_prague` (real public-transit
  time via IDOS.cz) directly onto `data/combined.json` **in place**. Must
  run after `geocode.py`, for the reason above.
- `building_type.py` (a pure classifier, no network) is instead called
  *from* `filter.py` itself, since it only needs already-scraped text.

## Site-specific facts (verified live, may drift - re-check before trusting)

- **chatyachalupy.cz** - the cleanest source. `/parts/cs/filtered-places?capacity=N&capacityMode=min&page=N`
  is a real server-side capacity filter returning paginated HTML cards.
  Detail pages expose exact lat/lon in `#place-map-data`'s
  `data-lon-lat-radius` attribute (`lon;lat;radius` order - don't swap it),
  and two real JSON availability endpoints:
  `/api/place/{id}/reserved-future-dates` and `/api/season/disabled-dates`.
  No robots.txt at all on this site.
- **e-chalupy.cz** - no capacity-filter param exists (the persons picker is
  a client-side-only React widget); candidates are found by crawling all 19
  region listing pages (`/{region}?p=N`) and reading capacity off each
  card. robots.txt disallows query strings except `?p=` - this project
  deliberately doesn't treat that as a hard blocker (see "robots.txt
  posture" below), but `?p=` pagination happens to be both the allowed
  mechanism and the one actually used. Detail pages have exact lat/lon in
  `<meta property="place:location:latitude/longitude">` and a real
  date-availability check via `POST https://api-pub.e-chalupy.cz/properties/{id}/demands/validate`.
  The regional crawl alone turns up ~2000 capacity>=20 candidates across
  the whole site - visiting every one for full detail is a lot of
  requests; the scraper caps detail-page visits (see its own docstring/log
  for the current cap and how candidates are prioritized when capped).
- **cs-chalupy.cz** - has ready-made capacity-band category pages
  (`/17-20-osob`, `/21-az-30-osob`, `/31-a-vice-osob`), which is a much
  smaller crawl than its full ~9300-page sitemap. Detail pages are served
  as `charset=windows-1250` - must decode with `cp1250`, not utf-8, or
  Czech text silently fails to match. Each detail page embeds its own
  per-day `#obsazenost` calendar (`obsazenost="0"`/`"3"` + a machine-readable
  `quick-date="YYYY-MM-DD"`, covering ~2.5 years ahead) - `parse_availability`
  reads it directly, no contact-the-owner guesswork needed. A listing that
  doesn't expose the widget at all still leaves `available_for_trip=None`.
  Does publish its own exact distance to the nearest bus/train stop on
  every detail page, so this source skips `geocode.py`'s Overpass lookup.
- **chata.cz** - `GET /vyhledavani/?Adults=N&FromDate=...&ToDate=...` is
  filtered server-side by both capacity and date availability at once
  (unique among the four). Results aren't in the visible HTML - they're
  embedded in a Google Tag Manager `dataLayer.push({ecommerce:{impressions:[...]}})`
  script block as `{id, name}` pairs; resolve real detail URLs by matching
  those ids against `sitemap.xml` (`-{ID}/` suffix on each `<loc>`). Has no
  scrapable price (dynamic JS booking widget, not static HTML).

- **idos.cz** (used by `transit.py`, not a rental site) - the plain results
  page `GET /vlakyautobusymhdvse/spojeni/vysledky/?f=<from>&fc=1&t=<to>&tc=1&date=DD.MM.YYYY&time=HH:MM`
  is fully server-rendered, no session/cookies/JS/CSRF token needed - "Celkovy
  cas" in the HTML is the total one-way journey time. robots.txt only
  disallows `/Ajax/` (not used here). The search wants a clean town/village
  name, not a full postal address - `transit.py::candidate_places()` strips
  street/house-number/postal-code noise, and falls back to a Nominatim
  reverse-geocode of the listing's own lat/lon when that still doesn't
  resolve (chatyachalupy.cz's `address_text` is often just a broad region
  label, not a real place - this fallback is what makes that source resolve
  at all).

## robots.txt posture for this project

The project owner explicitly instructed: ignore robots.txt restrictions
entirely for this one-off, private, non-commercial lookup, **including**
chata.cz's robots.txt naming and blocking `ClaudeBot` specifically (along
with GPTBot, ChatGPT, and several SEO bots) with a site-wide `Disallow: /`.
This was a conscious, explicit call on that specific site, made after it was
flagged as qualitatively different from a generic SEO-oriented disallow -
not a default to assume carries over to a new site or a new project without
asking again.

## Known data-quality gaps

Price semantics differ per site and are sometimes per-night, sometimes
per-weekend, sometimes per-week, sometimes **per-person rather than
whole-object** (a real trap: a cheap-looking "780 Kc/person/weekend" hotel
costs far more for a 25-person group than a flat "20 000 Kc whole object"
cottage) - `price_basis` on each record says what was actually found.
`filter.py`'s `estimate_trip_total()` classifies each record's basis text
(looks for "osob"/"objekt"/"noc"/"týden") and produces a `(low, high)` CZK
range rather than a single number - `_price_estimate_low_czk` assumes
`MIN_CAPACITY` guests, `_price_estimate_high_czk` assumes
`PREFERRED_CAPACITY`; both `None` means don't trust any number for that
record. Scoring uses the high (conservative) end. Treat these as estimates,
not quotes.

`transit.py`'s IDOS resolution rate is well under 100% - its own scraped
address text is frequently too vague (chatyachalupy.cz) or too specific
(a full postal address IDOS's place search can't parse) to resolve; the
Nominatim reverse-geocode fallback closes some but not all of that gap.
Unresolved listings keep `prague_distance_km` (always available wherever
coordinates exist) but have `transit_minutes_from_prague=None`.

`building_type` and `has_common_room` (see `building_type.py` and
`enrich_common_room.py` above) are both best-effort text classification,
not structured facts, except where a site exposes them directly (e-chalupy's
"N objektu" card tag and `icon-commonroom` marker; cs-chalupy.cz/chata.cz's
"společenská místnost" amenity checkbox). `unknown` means genuinely unknown,
never treat it as "no".

See README.md's "Known limitations" for the rest (per-site availability/
price gaps).
