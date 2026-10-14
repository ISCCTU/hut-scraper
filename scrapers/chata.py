"""chata.cz scraper.

The user explicitly told me to ignore robots.txt/bot-blocking for this one-off
personal scrape, including chata.cz's robots.txt naming ClaudeBot specifically
in a site-wide Disallow. Still pacing requests (~1/sec) as a courtesy.

Search endpoint (`/vyhledavani/?Adults=20&FromDate=...&ToDate=...`) filters
server-side by BOTH minimum capacity and date availability, which the other
three sites can't do in one shot. Pagination is NOT the `apage` hidden field
in the form (that param is ignored) -- the real click handler in the site's
own jvs.js bundle does `GET {url-without-page}&page={n}` returning a full
page whose <script> dataLayer.push({ecommerce:{impressions:[...]}}) block
lists that page's 10 results as {name, id}. Verified live: page=2 returns a
distinct set of ids from page=1.

Detail pages are resolved from result id (e.g. "CZ8727") via sitemap.xml,
where every listing URL ends in "-{ID}/".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from scrapers.common import (
    BUDGET_MAX_CZK,
    MIN_CAPACITY,
    TRIP_CHECKIN,
    TRIP_CHECKOUT,
    Hut,
    build_session,
    polite_get,
    save_json,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE = "https://www.chata.cz"
SEARCH_URL = f"{BASE}/vyhledavani/"
SITEMAP_URL = f"{BASE}/sitemap.xml"

IMPRESSION_RE = re.compile(
    r"'name':\s*'((?:[^'\\]|\\.)*)',\s*'id':\s*'([A-Z0-9]+)'", re.DOTALL
)
TOTAL_ITEMS_RE = re.compile(r"id=['\"]totalitems['\"]\s+value=(\d+)")
SITEMAP_ID_RE = re.compile(r"-([A-Z]{2}\d+)/?$")

CAPACITY_RE = re.compile(
    r"place-person-info-1[\"']>\s*Max\.\s*(\d+)\s*osob(?:,\s*(\d+)\s*pokoj)?"
)
GPS_RE = re.compile(r"GPS:\s*(-?\d+\.\d+)N,\s*(-?\d+\.\d+)E")
LOCATION_RE = re.compile(r'<div class="location">(.*?)</div>', re.DOTALL)
LOCATION_LINK_RE = re.compile(r'title="([^"]+)"[^>]*>\s*([^<]*)</a>')
NAME_RE = re.compile(r'<h1[^>]*place-name[^>]*>([^<]+)</h1>')
AMENITIES_HEADER_RE = re.compile(
    r"Doplňkové informace o chalupě</h2>\s*<div>\s*(.*?)\s*</div>", re.DOTALL
)
PRICE_RE = re.compile(r'<b>\s*([\d\s]{2,10})\s*Kč\s*</b>')


def fetch_search_ids(session) -> list[tuple[str, str]]:
    """Returns list of (id, name) tuples, deduped, across all pages."""
    results: dict[str, str] = {}
    params = {
        "Adults": str(MIN_CAPACITY),
        "FromDate": TRIP_CHECKIN.isoformat(),
        "ToDate": TRIP_CHECKOUT.isoformat(),
    }
    resp = polite_get(session, SEARCH_URL, params=params, delay=1.0)
    total = 71
    m = TOTAL_ITEMS_RE.search(resp.text)
    if m:
        total = int(m.group(1))
    logger.info("chata.cz search: %d total items reported", total)

    page = 1
    pages_needed = (total + 9) // 10
    while page <= pages_needed:
        if page == 1:
            html = resp.text
        else:
            p = dict(params)
            p["page"] = str(page)
            r = polite_get(session, SEARCH_URL, params=p, delay=1.0)
            html = r.text
        found_this_page = 0
        for m in IMPRESSION_RE.finditer(html):
            name, oid = m.group(1), m.group(2)
            if oid not in results:
                results[oid] = name
                found_this_page += 1
        logger.info("chata.cz search page %d: %d new ids (total so far %d)", page, found_this_page, len(results))
        if found_this_page == 0 and page > 1:
            break
        page += 1

    return list(results.items())


def build_sitemap_id_map(session) -> dict[str, str]:
    resp = polite_get(session, SITEMAP_URL, delay=0.5)
    locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
    id_map: dict[str, str] = {}
    for u in locs:
        m = SITEMAP_ID_RE.search(u)
        if m:
            id_map[m.group(1)] = u
    logger.info("chata.cz sitemap: %d urls, %d resolvable ids", len(locs), len(id_map))
    return id_map


def parse_detail(session, oid: str, name: str, url: str) -> Hut | None:
    resp = polite_get(session, url, delay=1.0)
    if resp.status_code != 200:
        logger.warning("chata.cz %s: detail fetch failed HTTP %d", oid, resp.status_code)
        return None
    html = resp.text

    cap_m = CAPACITY_RE.search(html)
    capacity = int(cap_m.group(1)) if cap_m else None
    rooms = int(cap_m.group(2)) if cap_m and cap_m.group(2) else None

    if capacity is None or capacity < MIN_CAPACITY:
        logger.info("chata.cz %s: capacity=%s below threshold or unparseable, dropping", oid, capacity)
        return None

    name_m = NAME_RE.search(html)
    real_name = name_m.group(1).strip() if name_m else name

    loc_m = LOCATION_RE.search(html)
    address_text, region = "", ""
    if loc_m:
        links = LOCATION_LINK_RE.findall(loc_m.group(1))
        if links:
            address_text = links[0][0]  # first <a title="..."> is the full "obec X, kraj Y, okres Z"
        if len(links) > 1:
            region = links[1][1].strip() or links[1][0]
            region = re.sub(r"^region\s+", "", region, flags=re.IGNORECASE)

    amenities: list[str] = []
    am_m = AMENITIES_HEADER_RE.search(html)
    if am_m:
        amenities = [a.strip() for a in am_m.group(1).split(",") if a.strip()]

    gps_m = GPS_RE.search(html)
    lat = float(gps_m.group(1)) if gps_m else None
    lon = float(gps_m.group(2)) if gps_m else None

    price_from = None
    price_basis = "chata.cz uses a dynamic per-guest/per-room booking widget; no static price found for this listing"
    price_m = PRICE_RE.search(html)
    if price_m:
        try:
            price_from = int(price_m.group(1).replace(" ", "").replace("\xa0", ""))
            price_basis = "first Kč amount found on page, unit/basis unconfirmed (may be a fee, not the room rate)"
        except ValueError:
            pass

    return Hut(
        source="chata.cz",
        source_id=oid,
        url=url,
        name=real_name,
        region=region,
        address_text=address_text,
        capacity=capacity,
        bedrooms=rooms,
        price_from_czk=price_from,
        price_basis=price_basis,
        amenities=amenities,
        available_for_trip=True,
        availability_note=(
            "derived from chata.cz search being date-filtered server-side "
            f"(Adults={MIN_CAPACITY}&FromDate={TRIP_CHECKIN.isoformat()}&ToDate={TRIP_CHECKOUT.isoformat()}); "
            "not independently re-verified per listing"
        ),
        lat=lat,
        lon=lon,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )


def run() -> list[Hut]:
    session = build_session()
    ids = fetch_search_ids(session)
    logger.info("chata.cz: %d distinct candidate ids from search", len(ids))

    id_map = build_sitemap_id_map(session)

    huts: list[Hut] = []
    for oid, name in ids:
        url = id_map.get(oid)
        if not url:
            logger.warning("chata.cz %s (%s): not found in sitemap, skipping", oid, name)
            continue
        try:
            hut = parse_detail(session, oid, name, url)
        except Exception:
            logger.exception("chata.cz %s: failed to parse detail page", oid)
            continue
        if hut:
            huts.append(hut)

    return huts


if __name__ == "__main__":
    huts = run()
    save_json(huts, "data/raw/chata.json")
    available = sum(1 for h in huts if h.available_for_trip)
    within_budget = sum(1 for h in huts if h.price_from_czk and h.price_from_czk <= BUDGET_MAX_CZK)
    print(f"chata.cz: {len(huts)} candidates (capacity>={MIN_CAPACITY}), {available} marked available for trip, "
          f"{within_budget} with a parsed price <= {BUDGET_MAX_CZK} Kc (most will have no parsed price)")
