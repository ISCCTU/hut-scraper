"""Scraper for cs-chalupy.cz.

Candidate discovery uses the site's own capacity-band category pages
(17-20-osob / 21-az-30-osob / 31-a-vice-osob) instead of crawling the
~9,300-listing sitemap -- cuts the request count by roughly 20x.

Pages are served as windows-1250, not UTF-8; every response must be decoded
with that codec or Czech diacritics silently break every text match.

Bonus found while building this: the site's own "Vzdálenosti" table row
gives exact walking distance to the nearest bus/train stop in meters, so
nearest_stop_* is populated straight from the source -- no geocoding needed
for this site.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta

from pipeline.config import TRIP_CHECKIN, TRIP_CHECKOUT
from scrapers.common import Hut, MIN_CAPACITY, build_session, polite_get, save_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE = "https://www.cs-chalupy.cz"
BANDS = ["/17-20-osob", "/21-az-30-osob", "/31-a-vice-osob"]

AMENITY_KEYWORDS = [
    "bazén", "sauna", "infrasauna", "vířivka", "whirlpool", "gril", "krb",
    "wifi", "koupací jezírko", "trampolína", "wellness", "kachlová kamna",
]


def fetch_cp1250(session, url, **kwargs) -> str:
    resp = polite_get(session, url, delay=0.4, **kwargs)
    resp.raise_for_status()
    return resp.content.decode("cp1250", errors="replace")


def discover_detail_urls(session) -> list[str]:
    urls: set[str] = set()
    for band in BANDS:
        page = 1
        while True:
            url = f"{BASE}{band}" if page == 1 else f"{BASE}{band}?p={page}"
            html = fetch_cp1250(session, url)
            found = set(re.findall(r'href="(https://www\.cs-chalupy\.cz/[a-z0-9-]+\.[0-9]+[A-Za-z]-[0-9]+\.html)"', html))
            if not found:
                break
            new = found - urls
            urls |= found
            logger.info("%s page %d: %d links (%d new, %d total)", band, page, len(found), len(new), len(urls))
            has_next_page = f"p={page + 1}" in html
            if not has_next_page:
                break
            page += 1
            if page > 30:
                logger.warning("safety cap hit on %s", band)
                break
    return sorted(urls)


def parse_capacity(html: str) -> int | None:
    m = re.search(r"kapacita:\s*<strong>\s*(\d+)\s*osob", html, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"kapacita\w*\s+(?:je\s+)?až\s+(\d+)\s*osob", html, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def parse_bedrooms(html: str) -> int | None:
    m = re.search(r"Rozložení ložnic:</strong></td><td>\s*(\d+)\s*ložnic", html, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'class="png png-bed"[^>]*>\s*<strong>\s*<a[^>]*>\s*(\d+)\s*ložnic', html, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def parse_equipment_amenities(html: str) -> list[str]:
    amenities: list[str] = []
    for label in ["Vybavení objektu - vnitřní", "Vybavení objektu - venkovní"]:
        m = re.search(re.escape(label) + r":</strong></td><td>([^<]*)</td>", html)
        if m:
            items = [x.strip() for x in m.group(1).split(",") if x.strip()]
            amenities.extend(items)
    if not amenities:
        text_lower = html.lower()
        amenities = [kw for kw in AMENITY_KEYWORDS if kw in text_lower]
    return amenities


def parse_distances(html: str) -> tuple[str, str, float | None]:
    m = re.search(r"Vzdálenosti:</strong></td><td>([^<]*)</td>", html)
    if not m:
        return "", "", None
    text = m.group(1)
    stops = re.findall(r"zastávka\s+(autobus|vlak)\s*\((\d+)\s*m\)", text, re.IGNORECASE)
    if not stops:
        return "", "", None
    best_type, best_m = min(stops, key=lambda t: int(t[1]))
    type_cs = {"autobus": "bus stop", "vlak": "train stop"}.get(best_type.lower(), best_type)
    return f"{type_cs} (cs-chalupy.cz stated distance)", type_cs, round(int(best_m) / 1000, 2)


def parse_price_table(html: str) -> tuple[int | None, str]:
    tables = re.findall(r'<table[^>]*class="[^"]*cenik[^"]*"[^>]*>(.*?)</table>', html, re.DOTALL)
    rows: list[tuple[str, int, str]] = []
    current_season = ""
    for table in tables:
        for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.DOTALL):
            season_m = re.search(r'sezona"[^>]*>\s*(?:<strong>)?\s*([^<]*?)\s*(?:</strong>)?\s*(?:<br\s*/?>)?\s*</td>', tr)
            if season_m and season_m.group(1).strip():
                current_season = season_m.group(1).strip()
            cena_m = re.search(r'cena r"[^>]*>\s*([\d\s]+)\s*Kč', tr)
            typ_m = re.search(r'typ">([^<]*)<', tr)
            if not (cena_m and typ_m and typ_m.group(1).strip()):
                continue
            # On a multi-room penzion/camp, cs-chalupy.cz's own table still labels a
            # per-room row "Objekt / ..." - only the adjoining "poznamka" note says
            # "cena za N lůžkový pokoj" (price for an N-bed ROOM). Verified live on
            # Penzion Dřevěnka Bozkov (6C-004): "850 Kč | Objekt / noc | ... cena za
            # 2 lůžkový pokoj" for a 22-capacity property - taking that at face value
            # as a whole-object rate understated the real cost by >10x. A genuine
            # whole-object row's note instead says things like "pronájem celé chaty"
            # or "objekt do N osob".
            poznamka_m = re.search(r'poznamka">([^<]*)<', tr)
            if poznamka_m and "pokoj" in poznamka_m.group(1).lower():
                continue
            price = int(cena_m.group(1).replace(" ", "").replace("\xa0", ""))
            rows.append((current_season, price, typ_m.group(1).strip()))

    if not rows:
        m = re.search(r'price price-from"[^>]*>[^<]*<cena>(\d+)</cena>', html)
        if m:
            return int(m.group(1)), "site-wide 'from' price shown on page, unit unclear"
        return None, ""

    weekend_rows = [r for r in rows if r[2] == "Objekt / víkend"]
    off_season_weekend = [r for r in weekend_rows if "mimosez" in r[0].lower()]
    if off_season_weekend:
        season, price, typ = min(off_season_weekend, key=lambda r: r[1])
        return price, f"whole-object weekend rate, off-season ({season}): {price} Kc"
    if weekend_rows:
        season, price, typ = min(weekend_rows, key=lambda r: r[1])
        return price, f"whole-object weekend rate, season={season} (off-season rate not listed separately): {price} Kc"

    night_rows = [r for r in rows if r[2] == "Objekt / noc"]
    off_season_night = [r for r in night_rows if "mimosez" in r[0].lower()]
    night_candidates = off_season_night or night_rows
    if night_candidates:
        season, price, typ = min(night_candidates, key=lambda r: r[1])
        total = price * 2
        return total, f"whole-object nightly rate x2 nights, season={season or 'n/a'}: {price} Kc/night -> {total} Kc for Fri-Sun"

    week_rows = [r for r in rows if r[2] == "Objekt / týden"]
    off_season_week = [r for r in week_rows if "mimosez" in r[0].lower()]
    candidates = off_season_week or week_rows
    if candidates:
        season, price, typ = min(candidates, key=lambda r: r[1])
        return price, f"WEEKLY (not weekend) whole-object rate, season={season}: {price} Kc -- no weekend rate listed"

    all_basis = "; ".join(f"{s or 'n/a'}: {p} Kc ({t})" for s, p, t in rows[:6])
    return None, f"no whole-object rate found, only per-person/other rates: {all_basis}"


def parse_address_and_geo(html: str) -> tuple[str, float | None, float | None]:
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "LodgingBusiness":
            continue
        address = data.get("address") or {}
        geo = data.get("geo") or {}
        addr_text = ", ".join(filter(None, [address.get("streetAddress"), address.get("addressLocality"), address.get("addressRegion")]))
        lat = float(geo["latitude"]) if geo.get("latitude") else None
        lon = float(geo["longitude"]) if geo.get("longitude") else None
        return addr_text, lat, lon
    return "", None, None


AVAILABILITY_RE = re.compile(r'obsazenost="(\d)"[^>]*?quick-date="(\d{4}-\d{2}-\d{2})"', re.DOTALL)


def parse_availability(html: str, checkin: date, checkout: date) -> tuple[bool | None, str]:
    """Read the #obsazenost calendar embedded in the detail page itself (0 = volno/free,
    3 = obsazeno/booked, per quick-date="YYYY-MM-DD") and check every night of the stay.
    This site DOES publish real per-listing availability -- contrary to this scraper's
    earlier assumption that it was contact-owner-only, which left available_for_trip
    permanently None. The widget covers ~2.5 years ahead on a live page, so a trip date
    missing from it means the calendar failed to parse, not that the night is unlisted.
    """
    statuses = {d: code for code, d in AVAILABILITY_RE.findall(html)}
    nights = []
    d = checkin
    while d < checkout:
        nights.append(d.isoformat())
        d += timedelta(days=1)
    missing = [n for n in nights if n not in statuses]
    if missing:
        return None, "availability calendar on the page did not cover the trip nights"
    booked = [n for n in nights if statuses[n] != "0"]
    if booked:
        return False, f"booked per the site's own calendar for: {', '.join(booked)}"
    return True, "free per the site's own calendar for all trip nights"


def scrape_detail(session, url: str) -> Hut | None:
    html = fetch_cp1250(session, url)
    capacity = parse_capacity(html)
    if capacity is None or capacity < MIN_CAPACITY:
        logger.info("skip %s: capacity=%s", url, capacity)
        return None

    code_m = re.search(r"\.([0-9]+[A-Za-z]-[0-9]+)\.html$", url)
    source_id = code_m.group(1) if code_m else url

    title_m = re.search(r"<title>([^<]*)</title>", html)
    name = title_m.group(1).split(":")[0].strip() if title_m else url

    address_text, lat, lon = parse_address_and_geo(html)
    if not address_text:
        slug = url.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        address_text = slug.replace("-", " ")

    price_from, price_basis = parse_price_table(html)
    stop_name, stop_type, stop_km = parse_distances(html)
    available_for_trip, availability_note = parse_availability(html, TRIP_CHECKIN, TRIP_CHECKOUT)

    return Hut(
        source="cs-chalupy.cz",
        source_id=source_id,
        url=url,
        name=name,
        address_text=address_text,
        capacity=capacity,
        bedrooms=parse_bedrooms(html),
        price_from_czk=price_from,
        price_basis=price_basis,
        amenities=parse_equipment_amenities(html),
        available_for_trip=available_for_trip,
        availability_note=availability_note,
        lat=lat,
        lon=lon,
        nearest_stop_name=stop_name,
        nearest_stop_type=stop_type,
        nearest_stop_distance_km=stop_km,
        scraped_at=datetime.now().isoformat(timespec="seconds"),
    )


OUTPUT_PATH = "data/raw/cs_chalupy.json"
SOURCE_ID_RE = re.compile(r"\.([0-9]+[A-Za-z]-[0-9]+)\.html$")
_HUT_FIELDS = {f.name for f in dataclasses.fields(Hut)}


def _load_existing() -> dict[str, Hut]:
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    # Filter to known Hut fields - an enrichment script may have patched extra keys
    # (e.g. rating_score, or a resume-sentinel key) directly onto this raw JSON file,
    # and Hut(**d) must not crash on a field it doesn't declare.
    return {
        d["source_id"]: Hut(**{k: v for k, v in d.items() if k in _HUT_FIELDS})
        for d in data
    }


def main() -> None:
    session = build_session()
    detail_urls = discover_detail_urls(session)
    logger.info("discovered %d unique detail URLs across all bands", len(detail_urls))

    existing = _load_existing()
    logger.info("%d candidates already scraped in a prior run, will be kept and not re-fetched", len(existing))

    huts: list[Hut] = list(existing.values())
    to_fetch = [u for u in detail_urls if not (m := SOURCE_ID_RE.search(u)) or m.group(1) not in existing]
    logger.info("%d URLs remaining to fetch (skipping %d already-known ids)", len(to_fetch), len(detail_urls) - len(to_fetch))

    for i, url in enumerate(to_fetch, 1):
        try:
            hut = scrape_detail(session, url)
        except Exception:
            logger.exception("failed on %s", url)
            continue
        if hut:
            huts.append(hut)
        if i % 20 == 0:
            save_json(huts, OUTPUT_PATH)
            logger.info("progress: %d/%d new detail pages, %d qualifying so far", i, len(to_fetch), len(huts))

    save_json(huts, OUTPUT_PATH)

    with_latlon = sum(1 for h in huts if h.lat is not None)
    with_price = sum(1 for h in huts if h.price_from_czk is not None)
    with_stop = sum(1 for h in huts if h.nearest_stop_distance_km is not None)
    booked = sum(1 for h in huts if h.available_for_trip is False)
    free = sum(1 for h in huts if h.available_for_trip is True)
    print(f"\ncandidates (capacity>={MIN_CAPACITY}): {len(huts)}")
    print(f"with lat/lon: {with_latlon}/{len(huts)}")
    print(f"with usable price: {with_price}/{len(huts)}")
    print(f"with stated stop distance: {with_stop}/{len(huts)}")
    print(f"availability for the trip: {free} free, {booked} booked, {len(huts) - free - booked} unknown")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
