"""Re-fetch every cs-chalupy.cz detail page and set available_for_trip from the site's
own #obsazenost calendar (parse_availability, scrapers/cs_chalupy.py) - this scraper
previously hardcoded available_for_trip=None under the wrong assumption that
cs-chalupy.cz had no public availability data. It does: every listing page embeds a
per-day calendar (obsazenost="0"/"3") covering ~2.5 years ahead.

Same fetch-failure-vs-confirmed-result trap as the rating enrichers: a page that
fails to fetch, or whose calendar doesn't cover the trip nights, must leave the
record unset so a resume pass retries it, rather than writing a false None that
looks identical to "checked, inconclusive" forever.

Patches data/raw/cs_chalupy.json in place; run filter.py again afterward.
"""
from __future__ import annotations

import json
import logging

from pipeline.config import TRIP_CHECKIN, TRIP_CHECKOUT
from scrapers.common import build_session
from scrapers.cs_chalupy import fetch_cp1250, parse_availability

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PATH = "data/raw/cs_chalupy.json"


class FetchFailed(Exception):
    pass


def check_page(session, url: str) -> tuple[bool | None, str]:
    try:
        html = fetch_cp1250(session, url)
    except Exception as exc:  # noqa: BLE001 - any fetch/HTTP failure
        raise FetchFailed from exc
    return parse_availability(html, TRIP_CHECKIN, TRIP_CHECKOUT)


def main() -> None:
    with open(PATH, encoding="utf-8") as f:
        records = json.load(f)
    session = build_session()

    # available_for_trip stays None for BOTH "never checked" and "calendar didn't cover
    # the trip nights" - availability_note distinguishes them: every checked outcome
    # (free, booked, or inconclusive) sets a non-empty note, so "never checked" is the
    # one state where both fields are still at their Hut-declared defaults.
    todo = [r for r in records if r.get("available_for_trip") is None and not r.get("availability_note")]
    skipped = len(records) - len(todo)
    if skipped:
        logger.info("%d/%d already checked in a prior run, skipping", skipped, len(records))

    free = sum(1 for r in records if r.get("available_for_trip") is True)
    booked = sum(1 for r in records if r.get("available_for_trip") is False)
    failed = 0
    for i, rec in enumerate(todo):
        try:
            available, note = check_page(session, rec["url"])
        except FetchFailed as exc:
            logger.warning("fetch failed for %s: %s", rec["url"], exc)
            failed += 1
            continue
        rec["available_for_trip"] = available
        rec["availability_note"] = note
        if available is True:
            free += 1
        elif available is False:
            booked += 1
            logger.info("BOOKED: %s (%s) -- %s", rec["source_id"], rec.get("name", ""), note)
        if (i + 1) % 25 == 0:
            with open(PATH, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            logger.info("checkpoint: %d/%d new records checked (%d failed, will retry) -- %d free, %d booked so far",
                        i + 1, len(todo), failed, free, booked)

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info("done: %d free, %d booked, %d unknown/uncovered, %d fetch failures left unset for retry -> %s",
                free, booked, len(records) - free - booked, failed, PATH)


if __name__ == "__main__":
    main()
