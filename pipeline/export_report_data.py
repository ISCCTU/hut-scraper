"""Trim data/filtered.json down to what the published report page needs, and
write it next to the page's other source files. Run after filter.py.
"""
from __future__ import annotations

import json
import sys


def trim(r: dict) -> dict:
    return {
        "source": r["source"],
        "id": r["source_id"],
        "url": r["url"],
        "name": r["name"],
        "region": r.get("region") or "",
        "address": r.get("address_text") or "",
        "capacity": r.get("capacity"),
        "bedrooms": r.get("bedrooms"),
        "priceLow": r.get("_price_estimate_low_czk"),
        "priceHigh": r.get("_price_estimate_high_czk"),
        "priceNote": r.get("_display_price_note") or "",
        "overBudget": r.get("_over_budget"),
        "amenities": r.get("amenities") or [],
        "available": r.get("available_for_trip"),
        "availNote": r.get("availability_note") or "",
        "distKm": r.get("nearest_stop_distance_km"),
        "stopName": r.get("nearest_stop_name") or "",
        "stopType": r.get("nearest_stop_type") or "",
        "pragueKm": r.get("prague_distance_km"),
        "transitMin": r.get("transit_minutes_from_prague"),
        "transitPlace": r.get("transit_query_place") or "",
        "buildingType": r.get("building_type") or "unknown",
        "buildingNote": r.get("building_type_note") or "",
        "commonRoom": r.get("has_common_room"),
        "commonRoomNote": r.get("common_room_note") or "",
        "score": r.get("_score"),
    }


def main(in_path: str, out_path: str) -> None:
    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)
    trimmed = [trim(r) for r in data]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(trimmed)} records to {out_path}")


if __name__ == "__main__":
    in_path = sys.argv[1] if len(sys.argv) > 1 else "data/filtered.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "report-data.json"
    main(in_path, out_path)
