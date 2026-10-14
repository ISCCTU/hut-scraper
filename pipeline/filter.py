"""Apply the trip's requirements to data/combined.json and produce a ranked shortlist.

Hard requirements: capacity >= 20 (min), not confirmed-booked for the trip dates.
Soft preferences (scored, not hard filters, since price/availability data quality
varies a lot across the four sites): capacity >= 30, price <= 30k, closer to a stop.
"""
from __future__ import annotations

import json
import logging

from pipeline.building_type import classify as classify_building
from pipeline.config import BUDGET_MAX_CZK, MAX_PRAGUE_DISTANCE_KM, MIN_CAPACITY, PREFERRED_CAPACITY, TRIP_NIGHTS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def estimate_trip_total(rec: dict) -> tuple[int | None, int | None, str]:
    """Turn whatever price signal was scraped into a (low, high) CZK estimate for
    the whole stay - not just a straight readout. Many 'from' prices turned out
    to be per-person rates on inspection (e.g. "780 Kc za osobu / vikend"), which
    look cheap but actually scale with group size - a 145-bed hotel at 720
    Kc/person/weekend costs far more for a group of 25 than a flat-rate cottage
    at 20 000 Kc whole-object. Returns (low, high, note); both None means "don't
    trust any number here", which callers must treat as unknown, not as free.
    """
    if rec.get("price_for_trip_czk") is not None:
        p = rec["price_for_trip_czk"]
        return p, p, "exact price for the trip dates"

    price = rec.get("price_from_czk")
    basis = (rec.get("price_basis") or "").lower()
    if price is None:
        return None, None, "no price data found"

    capacity = rec.get("capacity") or PREFERRED_CAPACITY
    low_guests = min(capacity, MIN_CAPACITY)
    high_guests = min(capacity, PREFERRED_CAPACITY)

    is_per_person = "osob" in basis  # "za osobu", "Osoba / noc", etc.
    # "objekt" catches e-chalupy.cz's raw Czech unit text ("za objekt"); "whole-object"
    # catches cs_chalupy.py, which already resolves per-person-vs-whole-object itself
    # (parse_price_table only ever returns a basis string for a confirmed whole-object
    # rate) and writes its own English basis text instead of passing the raw Czech through.
    is_whole_object = "objekt" in basis or "whole-object" in basis
    is_per_night = "noc" in basis
    is_per_week = "týden" in basis or "tyden" in basis or "weekly" in basis

    if is_whole_object and not is_per_person:
        total = price * TRIP_NIGHTS if is_per_night else price
        note = "whole-object rate" + (f" x {TRIP_NIGHTS} nights" if is_per_night else " (already covers the stay)")
        if is_per_week:
            note = "whole-object WEEKLY rate shown as-is - a 2-night stay may cost more or less, unconfirmed"
        return total, total, note

    if is_per_person:
        nights_factor = TRIP_NIGHTS if is_per_night else 1
        low = price * low_guests * nights_factor
        high = price * high_guests * nights_factor
        return low, high, f"PER-PERSON rate x {low_guests}-{high_guests} guests" + (f" x {TRIP_NIGHTS} nights" if nights_factor > 1 else " (rate already covers the stay)")

    return None, None, f"unit unconfirmed (raw 'from' price {price} Kc, basis: {rec.get('price_basis')!r}) - don't trust as a total"


def score(rec: dict, price_high: int | None) -> int:
    s = 0
    capacity = rec.get("capacity") or 0
    # Graduated, not a flat ">=30" bonus: peaks right at/just above PREFERRED_CAPACITY
    # and tapers off for much larger venues, which tend to be hostels/camps/hotels
    # rather than a private cottage for the group - a 145-bed "resort" isn't a better
    # match for a 20-30 person trip than a 32-bed cottage just because it's bigger.
    if capacity > PREFERRED_CAPACITY * 1.5:
        s += 1
    elif capacity >= PREFERRED_CAPACITY:
        s += 3
    elif capacity >= MIN_CAPACITY:
        s += 1 + round((capacity - MIN_CAPACITY) / (PREFERRED_CAPACITY - MIN_CAPACITY))

    avail = rec.get("available_for_trip")
    if avail is True:
        s += 3
    elif avail is None:
        s += 0
    # avail is False is filtered out entirely before scoring, see main()

    if price_high is not None:
        # score conservatively off the high end of the estimate (worst case: full 30 guests)
        s += 2 if price_high <= BUDGET_MAX_CZK else -2

    dist = rec.get("nearest_stop_distance_km")
    if dist is not None:
        if dist <= 1:
            s += 2
        elif dist <= 3:
            s += 1
        elif dist > 10:
            s -= 1

    transit = rec.get("transit_minutes_from_prague")
    if transit is not None:
        if transit <= 90:
            s += 2
        elif transit <= 150:
            s += 1
        elif transit > 240:
            s -= 1

    # Mild bonuses, not hard filters - both are heuristics/best-effort signals,
    # not confirmed facts, so they shouldn't dominate capacity/price/availability.
    if rec.get("building_type") == "single":
        s += 1
    if rec.get("has_common_room") is True:
        s += 2

    return s


def main() -> None:
    with open("data/combined.json", encoding="utf-8") as f:
        records = json.load(f)

    logger.info("loaded %d scraped records", len(records))

    kept = []
    dropped_capacity = dropped_booked = dropped_distance = 0
    for rec in records:
        capacity = rec.get("capacity")
        if capacity is None or capacity < MIN_CAPACITY:
            dropped_capacity += 1
            continue
        if rec.get("available_for_trip") is False:
            dropped_booked += 1
            continue
        if MAX_PRAGUE_DISTANCE_KM is not None:
            dist_prague = rec.get("prague_distance_km")
            if dist_prague is not None and dist_prague > MAX_PRAGUE_DISTANCE_KM:
                dropped_distance += 1
                continue
        building_kind, building_note = classify_building(rec)
        rec["building_type"] = building_kind
        rec["building_type_note"] = building_note
        # cs-chalupy.cz/chata.cz never got a dedicated has_common_room field - they
        # already have "společenská místnost" as a literal amenity when present, so
        # derive it the same way enrichment/common_room.py set it for the other two sites.
        if rec.get("has_common_room") is None:
            rec["has_common_room"] = "společenská místnost" in (rec.get("amenities") or [])
        price_low, price_high, price_note = estimate_trip_total(rec)
        rec["_price_estimate_low_czk"] = price_low
        rec["_price_estimate_high_czk"] = price_high
        rec["_display_price_note"] = price_note
        rec["_over_budget"] = price_high is not None and price_high > BUDGET_MAX_CZK
        rec["_score"] = score(rec, price_high)
        kept.append(rec)

    kept.sort(key=lambda r: (-r["_score"], -(r.get("capacity") or 0), r["_price_estimate_high_czk"] if r["_price_estimate_high_czk"] is not None else 10**9))

    logger.info(
        "kept %d candidates (dropped %d under capacity %d, %d confirmed booked, %d beyond MAX_PRAGUE_DISTANCE_KM=%s)",
        len(kept), dropped_capacity, MIN_CAPACITY, dropped_booked, dropped_distance, MAX_PRAGUE_DISTANCE_KM,
    )
    by_source: dict[str, int] = {}
    for r in kept:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    logger.info("by source: %s", by_source)

    with open("data/filtered.json", "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    logger.info("wrote %d ranked candidates -> data/filtered.json", len(kept))


if __name__ == "__main__":
    main()
