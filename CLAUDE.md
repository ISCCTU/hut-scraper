# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

A one-off personal scraper for four Czech cottage/hut-rental sites, built to
find a place for a group weekend trip. Filters on capacity, budget, and
distance to the nearest bus/train stop. Standalone - unrelated to any other
project on this machine, has its own git history.

## Commands

```bash
uv sync                           # install deps into .venv
uv run python cli.py all          # full pipeline: scrape -> enrich -> geocode -> transit -> filter -> export
uv run python cli.py fast         # scrape -> geocode -> filter -> export (skips the slow enrichers + transit.py)
uv run python cli.py scrape --only e_chalupy      # one scraper (also: chatyachalupy, cs_chalupy, chata)
uv run python cli.py enrich --only common-room,ratings,availability  # one or more enrich steps
uv run python cli.py geocode      # fill missing lat/lon, find nearest stop, write data/combined.json
uv run python cli.py transit      # add prague_distance_km + IDOS transit time to combined.json - AFTER geocode
uv run python cli.py filter       # apply pipeline/config.py's requirements, write data/filtered.json
uv run python cli.py export       # trim to report-data.json for the published report page

# Single-stage modules also run standalone with -m (never by bare file path -
# that breaks the package's absolute imports):
uv run python -m pipeline.geocode
uv run python -m pipeline.filter
uv run python -m enrichment.ratings            # all 3 sites
uv run python -m enrichment.ratings e_chalupy   # just one
```

`cli.py`'s `enrich` default order is `common-room`, `ratings`, `availability`
- all three patch `data/raw/*.json` in place and must finish before
`geocode`, which `cli.py all` enforces automatically. `reparse-prices` is
**not** in that default order (one-time migration, see Architecture below)
- run it explicitly via `--only reparse-prices` if ever needed again.
**`geocode` rebuilds `data/combined.json` from `data/raw/*.json` from
scratch and silently drops anything `transit` added** - `cli.py all`/`fast`
always call them in the right order, but if run as separate stages by hand,
always rerun `transit` again immediately after any `geocode` rerun.

`pipeline/config.py` holds the trip requirements (dates, capacity, budget,
origin city for distance/transit-time) - edit it or override any value with
an identically-named env var. See its docstring and README.md's table;
don't hardcode a new date/capacity/budget/origin constant anywhere else.

## Architecture

Three packages reflect three real categories, enforced by `cli.py` (see
Commands above) rather than only documented in prose:

- **`scrapers/`** - one independent module per site (`chatyachalupy.py`,
  `e_chalupy.py`, `cs_chalupy.py`, `chata.py`), each writing its own
  `data/raw/<site>.json` and rerunnable alone without touching the others.
  `scrapers/common.py` defines the shared `Hut` dataclass every scraper
  normalizes into, plus `build_session`/`polite_get`/`save_json` helpers.
  `cs_chalupy.py` and `e_chalupy.py` also resume-load their own prior output
  (`_load_existing()`, `Hut(**d)`) to skip already-known listings - that
  function filters incoming dict keys down to `Hut`'s declared fields first,
  so a stray key an enrichment script patched onto the raw JSON (anything
  not declared on `Hut`) can never crash the reload. `Hut` itself declares
  `rating_score`/`rating_count`/`rating_scale` for exactly this reason: they
  used to be patched on by the ratings enricher without being declared,
  which crashed this resume load the moment that enricher had run once.
- **`pipeline/`** - the core linear stages: `config.py` (trip knobs),
  `building_type.py` (pure text classifier, no network, called from
  `filter.py`), `geocode.py` (merges all four `data/raw/*.json` into
  `data/combined.json`, filling in `lat`/`lon` via Nominatim and the
  nearest bus/train stop via OSM Overpass - also patches any newly-resolved
  `lat`/`lon`/`nearest_stop_*` back onto each site's own `data/raw/<site>.json`,
  so a record resolved once is never re-queried against those shared public
  APIs on a later run), `transit.py` (adds
  `prague_distance_km` + real IDOS.cz transit time onto `combined.json` in
  place - must run after `geocode.py`, since `geocode.py` rebuilds that
  file from scratch every time), `filter.py` (drops anything under
  `MIN_CAPACITY` or confirmed booked, scores the rest, writes
  `data/filtered.json`), `export_report_data.py` (trims that down to what
  the published report page renders).
- **`enrichment/`** - slow, patch-`data/raw/*.json`-in-place scripts that
  must finish before `geocode.py` runs (since `geocode.py` only reads from
  `data/raw/`, anything added downstream of it is invisible to a rerun):
  `common_room.py` re-visits chatyachalupy.cz/e-chalupy.cz detail pages
  (their original scrapers' amenity keyword lists didn't include
  "společenská místnost" - cs-chalupy.cz/chata.cz already had it as a
  first-class checkbox) and patches `has_common_room`/`common_room_note`;
  `ratings.py` pulls each site's own guest-rating widget (merged from three
  near-identical per-site scripts into one `RatingSite`-config-driven
  module); `availability_cs_chalupy.py` reads cs-chalupy.cz's own
  `#obsazenost` calendar. **`reparse_cs_chalupy_prices.py` is a one-time
  migration, not a recurring stage** - it repaired `price_from_czk` on
  records scraped before a `cs_chalupy.py` price-table bug was fixed (see
  below); new scrapes already get the correct price directly, so it's
  deliberately excluded from `cli.py`'s default `enrich`/`all` order.

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
  every detail page, so this source skips `pipeline/geocode.py`'s Overpass lookup.
- **chata.cz** - `GET /vyhledavani/?Adults=N&FromDate=...&ToDate=...` is
  filtered server-side by both capacity and date availability at once
  (unique among the four). Results aren't in the visible HTML - they're
  embedded in a Google Tag Manager `dataLayer.push({ecommerce:{impressions:[...]}})`
  script block as `{id, name}` pairs; resolve real detail URLs by matching
  those ids against `sitemap.xml` (`-{ID}/` suffix on each `<loc>`). Has no
  scrapable price (dynamic JS booking widget, not static HTML).

- **idos.cz** (used by `pipeline/transit.py`, not a rental site) - the plain results
  page `GET /vlakyautobusymhdvse/spojeni/vysledky/?f=<from>&fc=1&t=<to>&tc=1&date=DD.MM.YYYY&time=HH:MM`
  is fully server-rendered, no session/cookies/JS/CSRF token needed - "Celkovy
  cas" in the HTML is the total one-way journey time. robots.txt only
  disallows `/Ajax/` (not used here). The search wants a clean town/village
  name, not a full postal address - `transit.py`'s `candidate_places()` strips
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
`pipeline/filter.py`'s `estimate_trip_total()` classifies each record's basis text
(looks for "osob"/"objekt"/"noc"/"týden") and produces a `(low, high)` CZK
range rather than a single number - `_price_estimate_low_czk` assumes
`MIN_CAPACITY` guests, `_price_estimate_high_czk` assumes
`PREFERRED_CAPACITY`; both `None` means don't trust any number for that
record. Scoring uses the high (conservative) end. Treat these as estimates,
not quotes.

`pipeline/transit.py`'s IDOS resolution rate is well under 100% - its own scraped
address text is frequently too vague (chatyachalupy.cz) or too specific
(a full postal address IDOS's place search can't parse) to resolve; the
Nominatim reverse-geocode fallback closes some but not all of that gap.
Unresolved listings keep `prague_distance_km` (always available wherever
coordinates exist) but have `transit_minutes_from_prague=None`.

`building_type` and `has_common_room` (see `pipeline/building_type.py` and
`enrichment/common_room.py` above) are both best-effort text classification,
not structured facts, except where a site exposes them directly (e-chalupy's
"N objektu" card tag and `icon-commonroom` marker; cs-chalupy.cz/chata.cz's
"společenská místnost" amenity checkbox). `unknown` means genuinely unknown,
never treat it as "no".

See README.md's "Known limitations" for the rest (per-site availability/
price gaps).
