"""Straight-line distance from ORIGIN_NAME (config.py, default Prague) for every
record with coordinates, plus real public-transit time via IDOS.cz's plain
GET results page - no session/cookies/JS needed, robots.txt only disallows
/Ajax/ (not used here). Run after geocode.py, before filter.py.

IDOS's own place search wants a clean town/village name, not a full postal
address - candidate_places() strips street+number/postal-code noise out of
address_text and tries a couple of fallbacks per record, since Czech address
strings vary a lot in how they order town vs. hamlet vs. district.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from pipeline.config import IDOS_DEPARTURE_TIME, ORIGIN_LAT, ORIGIN_LON, ORIGIN_NAME, TRIP_CHECKIN
from pipeline.geocode import haversine_km

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

IDOS_URL = "https://idos.cz/vlakyautobusymhdvse/spojeni/vysledky/"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
NOMINATIM_UA = "chalupy-hledani-personal-research/1.0 (one-off private trip planning script)"
DURATION_RE = re.compile(r"Celkov\S{1,3}\s*čas\s*<strong>\s*(?:(\d+)\s*hod)?\s*(?:(\d+)\s*min)?", re.IGNORECASE)

_POSTAL_PREFIX_RE = re.compile(r"^\d{3}\s?\d{2}\s+")
_STREET_NUMBER_RE = re.compile(r"^[A-Za-zÁ-Žá-žĚŠČŘŽÝÁÍÉÚŮĎŤŇěščřžýáíéúůďťň.\s]+\s+\d+[a-zA-Z0-9/]*$")
_EV_CISLO_RE = re.compile(r"ev\.?\s*č\.?", re.IGNORECASE)


def candidate_places(rec: dict) -> list[str]:
    """Ordered list of place-name guesses to try against IDOS, best first."""
    addr = (rec.get("address_text") or "").strip()
    region = (rec.get("region") or "").strip()
    out: list[str] = []
    for seg in (s.strip() for s in addr.split(",")):
        if not seg or re.fullmatch(r"\d{3}\s?\d{2}", seg):
            continue
        if _EV_CISLO_RE.search(seg):
            continue
        seg = _POSTAL_PREFIX_RE.sub("", seg)
        if _STREET_NUMBER_RE.match(seg):
            continue
        if seg not in out:
            out.append(seg)
    if region and region not in out and "/" not in region:  # "Orlicke hory / vychodni Cechy" isn't a place
        out.append(region)
    return out[:3]


def reverse_geocode_municipality(session: requests.Session, lat: float, lon: float) -> str | None:
    """Real municipality name for a coordinate, via Nominatim - IDOS resolves this
    reliably where a scraped address_text is missing or too broad (e.g. chatyachalupy.cz's
    address_text is often just a wide region label, not a real town)."""
    try:
        resp = session.get(
            NOMINATIM_REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 14, "accept-language": "cs"},
            headers={"User-Agent": NOMINATIM_UA},
            timeout=(10, 20),
        )
    except requests.RequestException as exc:
        logger.warning("nominatim reverse failed for (%s,%s): %s", lat, lon, exc)
        return None
    time.sleep(1.1)  # Nominatim usage policy: max 1 req/sec
    if resp.status_code != 200:
        return None
    addr = resp.json().get("address", {})
    return addr.get("municipality") or addr.get("town") or addr.get("village") or addr.get("city")


def query_idos(session: requests.Session, destination: str) -> int | None:
    """Total one-way minutes for the fastest ORIGIN_NAME -> destination connection
    on TRIP_CHECKIN, or None if IDOS didn't resolve the destination / found nothing."""
    try:
        resp = session.get(
            IDOS_URL,
            params={
                "f": ORIGIN_NAME, "fc": 1,
                "t": destination, "tc": 1,
                "date": TRIP_CHECKIN.strftime("%d.%m.%Y"),
                "time": IDOS_DEPARTURE_TIME,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=(10, 20),
        )
    except requests.RequestException as exc:
        logger.warning("idos request failed for %r: %s", destination, exc)
        return None
    if resp.status_code != 200:
        return None
    m = DURATION_RE.search(resp.text)
    if not m:
        return None
    hours = int(m.group(1)) if m.group(1) else 0
    minutes = int(m.group(2)) if m.group(2) else 0
    return hours * 60 + minutes


def main(in_path: str) -> None:
    session = requests.Session()
    with open(in_path, encoding="utf-8") as f:
        all_records: list[dict] = json.load(f)

    logger.info("loaded %d records from %s", len(all_records), in_path)

    for rec in all_records:
        if rec.get("lat") is not None and rec.get("lon") is not None:
            rec["prague_distance_km"] = round(haversine_km(ORIGIN_LAT, ORIGIN_LON, rec["lat"], rec["lon"]), 1)
        else:
            rec["prague_distance_km"] = None

    idos_cache: dict[str, int | None] = {}
    geocode_cache: dict[tuple[float, float], str | None] = {}
    resolved = failed = skipped = 0
    out_path = in_path
    todo = [r for r in all_records if "transit_minutes_from_prague" not in r]
    skipped = len(all_records) - len(todo)
    if skipped:
        logger.info("%d/%d already processed in a prior run, skipping", skipped, len(all_records))

    for i, rec in enumerate(todo):
        candidates = candidate_places(rec)
        minutes = None
        used = None
        for place in candidates:
            if place in idos_cache:
                minutes = idos_cache[place]
            else:
                minutes = query_idos(session, place)
                idos_cache[place] = minutes
                time.sleep(1.0)
            if minutes is not None:
                used = place
                break

        if minutes is None and rec.get("lat") is not None and rec.get("lon") is not None:
            grid_key = (round(rec["lat"], 2), round(rec["lon"], 2))  # ~1km grid, dedupes nearby huts
            if grid_key in geocode_cache:
                muni = geocode_cache[grid_key]
            else:
                muni = reverse_geocode_municipality(session, rec["lat"], rec["lon"])
                geocode_cache[grid_key] = muni
            if muni and muni not in idos_cache:
                idos_cache[muni] = query_idos(session, muni)
                time.sleep(1.0)
            if muni and idos_cache.get(muni) is not None:
                minutes = idos_cache[muni]
                used = muni

        rec["transit_minutes_from_prague"] = minutes
        rec["transit_query_place"] = used
        if minutes is not None:
            resolved += 1
        else:
            failed += 1

        if (i + 1) % 50 == 0:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(all_records, f, ensure_ascii=False, indent=2)
            logger.info("checkpoint: %d/%d new records processed (%d resolved, %d unresolved, %d distinct IDOS queries, %d reverse-geocodes)",
                        i + 1, len(todo), resolved, failed, len(idos_cache), len(geocode_cache))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    total_resolved = sum(1 for r in all_records if r.get("transit_minutes_from_prague") is not None)
    logger.info("done: %d/%d total records have a transit time (%d newly resolved this run, %d distinct IDOS queries, %d reverse-geocodes) -> %s",
                total_resolved, len(all_records), resolved, len(idos_cache), len(geocode_cache), out_path)


if __name__ == "__main__":
    main("data/combined.json")
