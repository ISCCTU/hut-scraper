"""Re-fetch every cs-chalupy.cz detail page and recompute price_from_czk/price_basis
with the fixed parse_price_table (scrapers/cs_chalupy.py) - the old version trusted
the table's "Typ" column alone, and on multi-room penzion/camp listings cs-chalupy.cz
labels a per-room row "Objekt / ..." even though the adjoining "poznamka" note says
"cena za N lůžkový pokoj" (price for a room). That understated some whole-property
prices by >10x (verified: Penzion Dřevěnka Bozkov showed 1700 Kc for a 22-person
property that is actually priced per-room). Patches data/raw/cs_chalupy.json in
place; run geocode.py + filter.py again afterward (not the site scrapers - capacity/
region/amenities/rating fields are untouched and correct).
"""
from __future__ import annotations

import json
import logging

from scrapers.common import build_session
from scrapers.cs_chalupy import fetch_cp1250, parse_price_table

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PATH = "data/raw/cs_chalupy.json"


class FetchFailed(Exception):
    pass


def main() -> None:
    with open(PATH, encoding="utf-8") as f:
        records = json.load(f)
    session = build_session()
    changed = 0
    failed = 0
    for i, rec in enumerate(records):
        try:
            html = fetch_cp1250(session, rec["url"])
        except Exception as exc:  # noqa: BLE001 - any fetch/HTTP failure, skip and leave record as-is
            logger.warning("fetch failed for %s: %s", rec["url"], exc)
            failed += 1
            continue
        new_price, new_basis = parse_price_table(html)
        old_price = rec.get("price_from_czk")
        if new_price != old_price:
            logger.info("%s: price_from_czk %r -> %r (%s)", rec["source_id"], old_price, new_price, new_basis[:90])
            changed += 1
        rec["price_from_czk"] = new_price
        rec["price_basis"] = new_basis
        if (i + 1) % 50 == 0:
            with open(PATH, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            logger.info("checkpoint: %d/%d re-checked (%d changed, %d fetch failures)", i + 1, len(records), changed, failed)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info("done: %d/%d re-checked, %d prices changed, %d fetch failures -> %s", len(records), len(records), changed, failed, PATH)


if __name__ == "__main__":
    main()
