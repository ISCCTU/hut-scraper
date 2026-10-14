"""Re-visit chatyachalupy.cz and e-chalupy.cz detail pages to check for a
"společenská místnost" (common/social room) - their scrapers' amenity keyword
lists didn't include it (cs-chalupy.cz and chata.cz already have it, since
their sites expose it as a first-class checkbox in their own equipment list).

Also flags when the site's own text calls it out as big ("velká společenská
místnost") - directly useful for "one big hut with a real common room" over
a handful of small cabins. Patches data/raw/chatyachalupy.json and
data/raw/e_chalupy.json in place; run before geocode.py.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from pipeline.config import DEFAULT_REQUEST_DELAY_SECONDS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
COMMON_ROOM_RE = re.compile(r"(velk\w*\s+)?společensk\w*\s+m[ií]stnost", re.IGNORECASE)


def check_page(session: requests.Session, url: str, *, scope_selector: str | None) -> tuple[bool, str]:
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 20))
    except requests.RequestException as exc:
        logger.warning("fetch failed for %s: %s", url, exc)
        return False, ""
    time.sleep(DEFAULT_REQUEST_DELAY_SECONDS)
    if resp.status_code != 200:
        return False, ""
    soup = BeautifulSoup(resp.text, "lxml")
    scope = soup.select_one(scope_selector) if scope_selector else soup
    text = (scope or soup).get_text(" ", strip=True)
    m = COMMON_ROOM_RE.search(text)
    if not m:
        return False, ""
    note = text[max(0, m.start() - 20):m.end() + 40].strip()
    return True, note


def enrich(path: str, *, scope_selector: str | None) -> None:
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    session = requests.Session()
    found = sum(1 for r in records if r.get("has_common_room") is True)
    # Hut.has_common_room defaults to None and is always present once asdict()'d, so a
    # bare "not in r" key check can never tell "never checked" apart from "checked, found
    # nothing" - both look identical. None is the actual "not yet checked" sentinel here.
    todo = [r for r in records if r.get("has_common_room") is None]
    skipped = len(records) - len(todo)
    if skipped:
        logger.info("%s: %d/%d already checked in a prior run, skipping", path, skipped, len(records))
    for i, rec in enumerate(todo):
        has_room, note = check_page(session, rec["url"], scope_selector=scope_selector)
        rec["has_common_room"] = has_room
        rec["common_room_note"] = note
        if has_room:
            found += 1
            if "společenská místnost" not in rec.get("amenities", []):
                rec.setdefault("amenities", []).append("společenská místnost")
        if (i + 1) % 25 == 0:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            logger.info("%s: %d/%d new records checked, %d with a common room total", path, i + 1, len(todo), found)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info("%s done: %d/%d have a společenská místnost", path, found, len(records))


def main() -> None:
    enrich("data/raw/chatyachalupy.json", scope_selector=None)  # whole page - the mention can be anywhere in the free-text description
    enrich("data/raw/e_chalupy.json", scope_selector="#vybaveni")


if __name__ == "__main__":
    main()
