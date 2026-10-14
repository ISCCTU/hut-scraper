# chalupy-hledani

One-off personal scrape of four Czech cottage/hut-rental sites (e-chalupy.cz,
chatyachalupy.cz, cs-chalupy.cz, chata.cz) to find a place for a group
weekend trip, filtered on capacity, budget, and distance to the nearest
bus/train stop. See `pipeline/config.py` for the actual trip requirements (dates,
minimum/preferred capacity, budget ceiling) - edit that file (or set the
matching environment variable) to reuse this for a different trip.

Standalone from the Bazos scraper project on purpose - unrelated domain, kept
out of that repo's tests/lint/git history.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
```

## Run

A single CLI (`cli.py`) replaces running each stage by hand. It bakes in
every ordering constraint that used to live only in prose here (each stage
mutates what the next one reads, so order matters):

```
uv run python cli.py all     # scrape -> enrich -> geocode -> transit -> filter -> export
uv run python cli.py fast    # scrape -> geocode -> filter -> export (skips the slow enrichers + transit.py)
```

`all` is the real full pipeline. `fast` exists because the enrichers and
`transit.py` are slow (one HTTP request per listing) - use it for a quick
rerun when you haven't changed anything those steps would need to pick up.

Re-running just one stage (e.g. after tweaking `config.py`'s budget):

```
uv run python cli.py filter && uv run python cli.py export
```

One scraper or one enrichment step only:

```
uv run python cli.py scrape --only e_chalupy
uv run python cli.py enrich --only common-room,ratings
```

`enrich`'s default order is `common-room`, `ratings`, `availability` - all
three patch `data/raw/*.json` in place and must finish before `geocode`.
`reparse-prices` is **not** in that default order: it was a one-time
migration that repaired records scraped before a `cs_chalupy.py` price-table
bug was fixed, and new scrapes already get the correct price directly. Run
it explicitly (`cli.py enrich --only reparse-prices`) only if a similar
upstream bug is ever found again.

Each pipeline module also still runs standalone with `-m`, the same
convention `scrapers/` already uses - never invoke a moved file by bare path
(e.g. `python pipeline/geocode.py`), since that breaks the package's
absolute imports:

```
uv run python -m pipeline.geocode
uv run python -m pipeline.filter
uv run python -m enrichment.ratings            # all 3 sites
uv run python -m enrichment.ratings e_chalupy   # just one
```

**Rerunning `pipeline/geocode.py` overwrites `data/combined.json` from
`data/raw/*.json` from scratch, discarding anything `transit.py` added** -
`cli.py all`/`fast` always call them in the right order automatically, but
if you run `geocode`/`transit` as separate stages by hand, always rerun
`transit` right after `geocode`, or the report loses travel-time data.

Output: `data/raw/*.json` (one file per site), `data/combined.json` (merged,
geocoded, transit-enriched), `data/filtered.json` (ranked shortlist meeting
the requirements in `pipeline/config.py`), `report-data.json` (trimmed for
the page).

## Configuration

`pipeline/config.py` holds the tunable knobs, each overridable via an
environment variable of the same name without touching code:

| Name | Default | Meaning |
|---|---|---|
| `TRIP_CHECKIN` / `TRIP_CHECKOUT` | 2026-11-13 / 2026-11-15 | the weekend being checked, ISO date |
| `MIN_CAPACITY` | 20 | hard minimum beds/persons |
| `PREFERRED_CAPACITY` | 30 | scored as a bonus, not a hard filter |
| `BUDGET_MAX_CZK` | 30000 | whole-object price ceiling for the whole stay |
| `DEFAULT_REQUEST_DELAY_SECONDS` | 1.0 | pause between requests in the scrapers |
| `ORIGIN_NAME` | `Praha` | where the group travels from - IDOS.cz `f=` search text |
| `ORIGIN_LAT` / `ORIGIN_LON` | Prague center | for the straight-line `prague_distance_km` field |
| `IDOS_DEPARTURE_TIME` | `15:00` | departure time used for the IDOS.cz journey query |
| `MAX_PRAGUE_DISTANCE_KM` | unset (no cap) | hard filter in `filter.py`; set to narrow the search radius |

Example: `MIN_CAPACITY=15 BUDGET_MAX_CZK=20000 uv run python cli.py filter`
re-filters already-scraped data without rerunning the scrapers.

## Known limitations

- Price fields are inconsistent across sites (per night / per weekend / per
  week / per room, and sometimes **per-person rather than whole-object** -
  a cheap-looking per-person rate can total more than a flat cottage rate
  for a real group). `filter.py` classifies each record's `price_basis`
  text and outputs `_price_estimate_low_czk`/`_price_estimate_high_czk`
  (assuming `MIN_CAPACITY`/`PREFERRED_CAPACITY` guests respectively)
  instead of a single number - treat both as estimates, not quotes.
- cs-chalupy.cz publishes a real per-day availability calendar on every
  detail page (`#obsazenost`); `scrapers/cs_chalupy.py::parse_availability`
  reads it directly against `TRIP_CHECKIN`/`TRIP_CHECKOUT`. A listing that
  doesn't expose the widget at all still leaves `available_for_trip=None`
  (unknown, not "unavailable") - run `uv run python cli.py enrich --only
  availability` to (re-)check already-scraped records without re-scraping
  everything else. It does, however, publish exact walking distance to the
  nearest bus/train stop itself, so those records skip the geocoding step
  entirely.
- chata.cz has no scrapable price field at all (dynamic JS booking widget,
  not static HTML) - `price_from_czk` is always null there. Its results are
  marked `available_for_trip=True` based on the search itself already being
  date-filtered server-side, not independently re-verified per listing.
- e-chalupy.cz has no capacity-filter search - every candidate is found by
  crawling all 19 regions' listing pages and filtering in Python, which
  turned up ~2000 candidates for capacity>=20 alone. Detail-page visits
  there are capped (see the scraper's own log/report for the exact count
  and which capacity range got covered) rather than visiting every one.
- chata.cz's robots.txt explicitly names and blocks `ClaudeBot` (plus
  several other AI/SEO bots); this was a deliberate, explicit call by the
  project owner for this one-off personal, non-commercial lookup - not a
  default posture to carry into other projects.
- `transit.py`'s IDOS.cz travel-time lookup only resolves a fraction of
  listings - its own scraped address text is often too vague (a bare region
  name) or too specific (a full postal address with house number) for
  IDOS's place search, and the Nominatim reverse-geocode fallback doesn't
  always turn up a name IDOS recognizes either. Unresolved listings fall
  back to the straight-line `prague_distance_km`, which is always populated
  wherever coordinates exist.
- `building_type` (single building vs. multiple small units) and
  `has_common_room` are both **inferred from listing text**, not structured
  site data (except e-chalupy.cz's own "N objektu" card tag and its
  `icon-commonroom` marker, and cs-chalupy.cz/chata.cz's "společenská
  místnost" amenity checkbox, which are read directly). Treat `unknown`
  as genuinely unknown, not "no".
