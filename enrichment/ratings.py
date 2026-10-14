"""Pull each site's own guest-rating widget (score + review count). Merges what
used to be three near-identical scripts (one per site) into one, varying only by
target file, regex, scale, and whether a separate "confirmed zero reviews" marker
exists outside the main regex match.

Same fetch-failure-vs-confirmed-result trap in all three sites: a page that fails
to fetch or doesn't match any known rating shape must leave the record unset so a
resume pass retries it, rather than writing a false zero that looks identical to
"checked, no rating" forever.

Patches data/raw/{e_chalupy,chata,cs_chalupy}.json in place; run before geocode.py
(which rebuilds data/combined.json from the raw files on every run, so anything
added only to combined.json/filtered.json would be lost on the next full refresh).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass

import requests

from pipeline.config import DEFAULT_REQUEST_DELAY_SECONDS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class FetchFailed(Exception):
    """Page could not be fetched/parsed at all - distinct from a confirmed zero-rating
    page, so a resume pass retries it instead of locking in a false None forever."""


@dataclass(frozen=True)
class RatingSite:
    key: str                  # data/raw/<key>.json
    name: str                 # for log messages, e.g. "e-chalupy.cz"
    scale: int                # 5 or 10
    pattern: re.Pattern[str]  # named groups: score, optional count
    zero_pattern: re.Pattern[str] | None = None  # checked only if `pattern` doesn't match at all

    @property
    def path(self) -> str:
        return f"data/raw/{self.key}.json"


SITES: dict[str, RatingSite] = {
    "e_chalupy": RatingSite(
        key="e_chalupy", name="e-chalupy.cz", scale=5,
        # The "item rating" widget appears twice (header + specs grid) with identical
        # markup; either match is fine. "Bez hodnoceni" (no rating) is the confirmed-zero
        # branch of the SAME match - the count group simply doesn't participate on it.
        pattern=re.compile(
            r'<div class="item rating">.*?<span class="v">(?P<score>[\d,]+)</span>.*?>\s*'
            r'(?:Bez hodnocen[ií]|(?P<count>\d+)\s*(?:hodnocen\w*|recenz\w*))\s*</a>',
            re.DOTALL,
        ),
    ),
    "chata": RatingSite(
        key="chata", name="chata.cz", scale=10,
        pattern=re.compile(
            r'<span class="head-rating">[^<]*?(?P<score>[\d,]+)</span>\s*'
            r'<span class="head-rating-small">[^<]*</span>.*?podle\s*(?P<count>\d+)\s*recenz',
            re.DOTALL,
        ),
        zero_pattern=re.compile(r'class="no-rating"'),
    ),
    "cs_chalupy": RatingSite(
        key="cs_chalupy", name="cs-chalupy.cz", scale=10,
        pattern=re.compile(
            r'<h4><i class="fa fa-star"></i>\s*(?P<score>[\d.,]+)\s*</h4>.*?Počet recenz[ií]:\s*(?P<count>\d+)',
            re.DOTALL,
        ),
    ),
}
SITE_ORDER = ["e_chalupy", "chata", "cs_chalupy"]


def check_page(session: requests.Session, url: str, site: RatingSite) -> tuple[float, int]:
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 20))
    except requests.RequestException as exc:
        logger.warning("%s: fetch failed for %s: %s", site.name, url, exc)
        raise FetchFailed from exc
    time.sleep(DEFAULT_REQUEST_DELAY_SECONDS)
    if resp.status_code != 200:
        raise FetchFailed(f"status {resp.status_code}")

    m = site.pattern.search(resp.text)
    if m:
        count_str = m.groupdict().get("count")
        if count_str is None:
            return 0.0, 0
        return float(m.group("score").replace(",", ".")), int(count_str)

    if site.zero_pattern and site.zero_pattern.search(resp.text):
        return 0.0, 0

    raise FetchFailed("rating widget not found in page")


def enrich(site: RatingSite) -> None:
    with open(site.path, encoding="utf-8") as f:
        records = json.load(f)
    session = requests.Session()
    # rating_score is now a declared Hut field (default None), so a freshly-scraped
    # record already carries the key with a null value via asdict()/save_json() - a
    # bare "not in r" key check can never tell "never checked" apart from "checked,
    # found nothing" once that's true, exactly the has_common_room trap documented in
    # enrichment/common_room.py. A confirmed zero-rating page returns (0.0, 0), never
    # None, so None is the correct "not yet checked" sentinel here too.
    todo = [r for r in records if r.get("rating_score") is None]
    skipped = len(records) - len(todo)
    if skipped:
        logger.info("%s: %d/%d already checked in a prior run, skipping", site.name, skipped, len(records))
    found = sum(1 for r in records if r.get("rating_count"))
    failed = 0
    for i, rec in enumerate(todo):
        try:
            score, count = check_page(session, rec["url"], site)
        except FetchFailed:
            failed += 1
            continue  # leave "rating_score" unset so a future run retries this one
        rec["rating_score"] = score
        rec["rating_count"] = count
        rec["rating_scale"] = site.scale
        if count:
            found += 1
        if (i + 1) % 25 == 0:
            with open(site.path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            logger.info("%s: %d/%d new records checked (%d failed, will retry), %d with a real rating total",
                        site.name, i + 1, len(todo), failed, found)
    with open(site.path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info("%s done: %d/%d have a nonzero rating (%d fetch failures left unset for retry)",
                site.name, found, len(records), failed)


def main(sites: list[str] | None = None) -> None:
    for key in (sites or SITE_ORDER):
        enrich(SITES[key])


if __name__ == "__main__":
    main(sys.argv[1:] or None)
