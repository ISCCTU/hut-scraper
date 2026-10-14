"""Trip-specific knobs. Edit the defaults below to reuse this project for a
different trip, or override any of them with an environment variable of the
same name without touching code (e.g. `MIN_CAPACITY=15 python filter.py`).
"""
from __future__ import annotations

import os
from datetime import date

__all__ = [
    "TRIP_CHECKIN",
    "TRIP_CHECKOUT",
    "TRIP_NIGHTS",
    "MIN_CAPACITY",
    "PREFERRED_CAPACITY",
    "BUDGET_MAX_CZK",
    "DEFAULT_REQUEST_DELAY_SECONDS",
    "ORIGIN_NAME",
    "ORIGIN_LAT",
    "ORIGIN_LON",
    "IDOS_DEPARTURE_TIME",
    "MAX_PRAGUE_DISTANCE_KM",
]


def _date_env(name: str, default: date) -> date:
    raw = os.environ.get(name)
    return date.fromisoformat(raw) if raw else default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


# The weekend being checked (Friday check-in, Sunday check-out by default).
TRIP_CHECKIN = _date_env("TRIP_CHECKIN", date(2026, 11, 13))
TRIP_CHECKOUT = _date_env("TRIP_CHECKOUT", date(2026, 11, 15))
TRIP_NIGHTS = (TRIP_CHECKOUT - TRIP_CHECKIN).days

# Capacity, in beds/persons (what these sites call "osob").
MIN_CAPACITY = _int_env("MIN_CAPACITY", 17)
PREFERRED_CAPACITY = _int_env("PREFERRED_CAPACITY", 30)

# Budget ceiling, whole-object total for the whole stay, in CZK.
BUDGET_MAX_CZK = _int_env("BUDGET_MAX_CZK", 30_000)

# Default pause between HTTP requests in the scrapers, in seconds.
DEFAULT_REQUEST_DELAY_SECONDS = _float_env("DEFAULT_REQUEST_DELAY_SECONDS", 1.0)

# Where the group travels from - used for straight-line distance and for the
# IDOS.cz "f=" (from) journey-search parameter. "Praha" resolves fine on IDOS
# as a city-wide search across all its stations, which is what we want.
ORIGIN_NAME = os.environ.get("ORIGIN_NAME", "Praha")
ORIGIN_LAT = _float_env("ORIGIN_LAT", 50.0755)
ORIGIN_LON = _float_env("ORIGIN_LON", 14.4378)

# Friday-evening-ish departure time for the IDOS query (24h "HH:MM") - affects
# which timetabled connections show up, not just how long they take.
IDOS_DEPARTURE_TIME = os.environ.get("IDOS_DEPARTURE_TIME", "15:00")

# Optional hard cap on straight-line km from ORIGIN_NAME. None (default) means
# no cap - matches "anywhere in Czech Republic". Set an env var to narrow it,
# e.g. MAX_PRAGUE_DISTANCE_KM=150.
_max_dist_raw = os.environ.get("MAX_PRAGUE_DISTANCE_KM")
MAX_PRAGUE_DISTANCE_KM = float(_max_dist_raw) if _max_dist_raw else None
