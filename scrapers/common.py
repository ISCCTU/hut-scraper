"""Shared record schema and HTTP helpers for all four site scrapers."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field

import requests

from pipeline.config import (
    BUDGET_MAX_CZK,
    DEFAULT_REQUEST_DELAY_SECONDS,
    MIN_CAPACITY,
    PREFERRED_CAPACITY,
    TRIP_CHECKIN,
    TRIP_CHECKOUT,
    TRIP_NIGHTS,
)

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# Re-exported from config.py so existing `from scrapers.common import ...` call
# sites keep working — config.py is the source of truth, edit values there.
__all__ = [
    "TRIP_CHECKIN",
    "TRIP_CHECKOUT",
    "TRIP_NIGHTS",
    "MIN_CAPACITY",
    "PREFERRED_CAPACITY",
    "BUDGET_MAX_CZK",
    "Hut",
    "build_session",
    "polite_get",
    "save_json",
]


@dataclass
class Hut:
    source: str
    source_id: str
    url: str
    name: str
    region: str = ""
    address_text: str = ""
    capacity: int | None = None
    bedrooms: int | None = None
    price_from_czk: int | None = None
    price_basis: str = ""  # free text: what the number actually means (e.g. "from-price per night, unit unclear")
    price_for_trip_czk: int | None = None  # exact total for TRIP_CHECKIN..TRIP_CHECKOUT, when the site tells us
    amenities: list[str] = field(default_factory=list)
    description_snippet: str = ""
    available_for_trip: bool | None = None  # None = unknown / couldn't determine, not "no"
    availability_note: str = ""
    lat: float | None = None
    lon: float | None = None
    nearest_stop_name: str = ""
    nearest_stop_type: str = ""
    nearest_stop_distance_km: float | None = None
    scraped_at: str = ""
    # Patched in later, directly onto data/raw/*.json, by enrichment/common_room.py and
    # enrichment/ratings.py (not set by any scraper itself) - declared here anyway so
    # Hut(**d) round-trips a saved record without TypeError during a resume load,
    # regardless of which enrichers have already run against this file. _load_existing()
    # in cs_chalupy.py/e_chalupy.py also defensively filters to known field names before
    # calling Hut(**d), so a *future* stray enrichment key can't reintroduce this crash.
    has_common_room: bool | None = None
    common_room_note: str = ""
    rating_score: float | None = None
    rating_count: int | None = None
    rating_scale: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "cs,en;q=0.8"})
    return s


def polite_get(session: requests.Session, url: str, *, params: dict | None = None, delay: float = DEFAULT_REQUEST_DELAY_SECONDS, **kwargs) -> requests.Response:
    resp = session.get(url, params=params, timeout=(10, 20), **kwargs)
    time.sleep(delay)
    return resp


def save_json(huts: list[Hut], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([h.to_dict() for h in huts], f, ensure_ascii=False, indent=2, default=str)
    logger.info("wrote %d records to %s", len(huts), path)
