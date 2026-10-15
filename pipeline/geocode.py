"""Fill in missing lat/lon (Nominatim fallback) and find the nearest bus/train stop
(Overpass/OSM) for every hut. Run after the site scrapers, before filter.py.

Most sites already give exact coordinates; Nominatim is only a fallback for the rest.
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "chalupy-hledani-personal-research/1.0 (one-off private trip planning script)"

STOP_SEARCH_RADII_M = [3000, 8000, 20000]  # widen if nothing found nearby (rural huts)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_nominatim(session: requests.Session, address_text: str) -> tuple[float, float] | None:
    if not address_text:
        return None
    resp = session.get(
        NOMINATIM_URL,
        params={"q": f"{address_text}, Czech Republic", "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=(10, 20),
    )
    time.sleep(1.1)  # Nominatim usage policy: max 1 req/sec
    if resp.status_code != 200:
        return None
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


_overpass_consecutive_failures = 0
_overpass_disabled = False
OVERPASS_FAILURE_LIMIT = 3  # after this many connection failures in a row, stop trying for the rest of the run


def nearest_stop(session: requests.Session, lat: float, lon: float) -> tuple[str, str, float] | None:
    global _overpass_consecutive_failures, _overpass_disabled
    if _overpass_disabled:
        return None
    for radius in STOP_SEARCH_RADII_M:
        query = f"""
        [out:json][timeout:25];
        (
          node["highway"="bus_stop"](around:{radius},{lat},{lon});
          node["public_transport"="stop_position"](around:{radius},{lat},{lon});
          node["railway"="station"](around:{radius},{lat},{lon});
          node["railway"="halt"](around:{radius},{lat},{lon});
        );
        out body;
        """
        try:
            resp = session.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=(6, 20))
        except requests.RequestException as exc:
            _overpass_consecutive_failures += 1
            logger.warning("overpass request failed for (%s,%s) r=%d [%d/%d]: %s",
                            lat, lon, radius, _overpass_consecutive_failures, OVERPASS_FAILURE_LIMIT, exc)
            if _overpass_consecutive_failures >= OVERPASS_FAILURE_LIMIT:
                _overpass_disabled = True
                logger.warning("overpass unreachable %d times in a row - disabling nearest-stop lookups for the rest of this run", OVERPASS_FAILURE_LIMIT)
                return None
            time.sleep(1.2)
            continue
        _overpass_consecutive_failures = 0
        time.sleep(1.2)  # be polite to the shared public Overpass instance
        if resp.status_code != 200:
            logger.warning("overpass %s returned %s for (%s,%s) r=%d", OVERPASS_URL, resp.status_code, lat, lon, radius)
            continue
        elements = resp.json().get("elements", [])
        if not elements:
            continue
        best = None
        for el in elements:
            d = haversine_km(lat, lon, el["lat"], el["lon"])
            if best is None or d < best[0]:
                tags = el.get("tags", {})
                stop_type = "train" if tags.get("railway") in ("station", "halt") else "bus"
                name = tags.get("name", "(unnamed stop)")
                best = (d, name, stop_type)
        if best:
            d, name, stop_type = best
            return name, stop_type, round(d, 2)
    return None


def main(paths: list[str]) -> None:
    session = requests.Session()
    # Keep each site's records in their own list (not just one flat all_records
    # list) so a resolved lat/lon/nearest_stop_* can be checkpointed back onto
    # that site's own data/raw/*.json, not just the combined.json this function
    # already rebuilds from scratch every run. Records are dicts, so a rec
    # mutated via all_records below mutates the same object held in per_path -
    # no separate patching pass needed. Without this, every full pipeline run
    # re-queries the shared public Nominatim/Overpass APIs for every record
    # those sites don't already publish coordinates/stops for, forever.
    per_path: dict[str, list[dict]] = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            per_path[path] = json.load(f)
    all_records: list[dict] = [rec for records in per_path.values() for rec in records]

    out_path = "data/combined.json"

    def checkpoint() -> None:
        for raw_path, records in per_path.items():
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(all_records, f, ensure_ascii=False, indent=2)

    geocoded = 0
    stops_found = 0
    for i, rec in enumerate(all_records):
        if rec.get("nearest_stop_distance_km") is not None:
            continue  # cs-chalupy.cz already publishes this - no need to query Overpass

        if rec.get("lat") is None or rec.get("lon") is None:
            try:
                coords = geocode_nominatim(session, rec.get("address_text", "") or rec.get("region", ""))
            except requests.RequestException as exc:
                logger.warning("nominatim request failed for %s (%s): %s", rec["source_id"], rec["source"], exc)
                coords = None
            if coords:
                rec["lat"], rec["lon"] = coords
                geocoded += 1
                logger.info("geocoded %s (%s) -> %s,%s", rec["source_id"], rec["source"], *coords)
            else:
                logger.info("could not geocode %s (%s), address=%r", rec["source_id"], rec["source"], rec.get("address_text"))
                continue

        try:
            stop = nearest_stop(session, rec["lat"], rec["lon"])
        except requests.RequestException as exc:
            logger.warning("overpass lookup failed for %s (%s): %s", rec["source_id"], rec["source"], exc)
            stop = None
        if stop:
            rec["nearest_stop_name"], rec["nearest_stop_type"], rec["nearest_stop_distance_km"] = stop
            stops_found += 1
        else:
            logger.info("no stop found near %s (%s) within %dkm", rec["source_id"], rec["source"], STOP_SEARCH_RADII_M[-1] // 1000)

        if (i + 1) % 50 == 0:
            checkpoint()
            logger.info("checkpoint: %d/%d records processed", i + 1, len(all_records))

    checkpoint()

    logger.info(
        "done: %d records, %d newly geocoded, %d with a nearest stop resolved -> %s "
        "(lat/lon/nearest_stop_* also patched back onto each site's data/raw/*.json)",
        len(all_records), geocoded, stops_found, out_path,
    )


if __name__ == "__main__":
    paths = sys.argv[1:] or [
        "data/raw/chatyachalupy.json",
        "data/raw/e_chalupy.json",
        "data/raw/cs_chalupy.json",
        "data/raw/chata.json",
    ]
    existing = [p for p in paths if __import__("os").path.exists(p)]
    missing = set(paths) - set(existing)
    if missing:
        logger.warning("skipping missing files (scraper not finished yet?): %s", sorted(missing))
    main(existing)
