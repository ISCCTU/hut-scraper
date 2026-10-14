"""Pull cs-chalupy.cz's own guest-rating widget (score 0-10 + review count).
Same pattern as enrich_ratings.py (e-chalupy.cz) and the same trap applies:
a fetch failure must leave the record unset, not write a false zero, or a
resume pass can never tell "never checked" apart from "checked, no rating".
Patches data/raw/cs_chalupy.json in place; run before geocode.py.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from config import DEFAULT_REQUEST_DELAY_SECONDS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
RATING_RE = re.compile(
    r'<h4><i class="fa fa-star"></i>\s*([\d.,]+)\s*</h4>.*?Počet recenz[ií]:\s*(\d+)',
    re.DOTALL,
)


class FetchFailed(Exception):
    pass


def check_page(session: requests.Session, url: str) -> tuple[float, int]:
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 20))
    except requests.RequestException as exc:
        logger.warning("fetch failed for %s: %s", url, exc)
        raise FetchFailed from exc
    time.sleep(DEFAULT_REQUEST_DELAY_SECONDS)
    if resp.status_code != 200:
        raise FetchFailed(f"status {resp.status_code}")
    m = RATING_RE.search(resp.text)
    if not m:
        raise FetchFailed("rating widget not found in page")
    score = float(m.group(1).replace(",", "."))
    count = int(m.group(2))
    return score, count


def enrich(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    session = requests.Session()
    todo = [r for r in records if "rating_score" not in r]
    skipped = len(records) - len(todo)
    if skipped:
        logger.info("%s: %d/%d already checked in a prior run, skipping", path, skipped, len(records))
    found = sum(1 for r in records if r.get("rating_count"))
    failed = 0
    for i, rec in enumerate(todo):
        try:
            score, count = check_page(session, rec["url"])
        except FetchFailed:
            failed += 1
            continue
        rec["rating_score"] = score
        rec["rating_count"] = count
        rec["rating_scale"] = 10
        if count:
            found += 1
        if (i + 1) % 25 == 0:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            logger.info("%s: %d/%d new records checked (%d failed, will retry), %d with a real rating total",
                        path, i + 1, len(todo), failed, found)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info("%s done: %d/%d have a nonzero rating (%d fetch failures left unset for retry)",
                path, found, len(records), failed)


if __name__ == "__main__":
    enrich("data/raw/cs_chalupy.json")
