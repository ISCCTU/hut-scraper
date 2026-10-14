"""Run the core pipeline: scrape all four sites, geocode/find nearest stops,
filter+rank against config.py's requirements, export the report data file.

Deliberately excludes enrich_common_room.py and transit.py - both are slow
(one HTTP request per listing, ~10-20 min each) and enrich_common_room.py
must run *before* geocode.py while transit.py must run *after* it (see
README.md), so they don't fit this otherwise-linear pipeline cleanly. Run
them by hand, in that order, then rerun geocode.py -> this script again.

Each scraper stage is independent and writes its own file under data/, so a
failed site scraper doesn't block the others - rerun just that one module
directly (`uv run python -m scrapers.e_chalupy`) and then rerun from geocode
onward.
"""
from __future__ import annotations

import logging
import runpy
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SCRAPER_MODULES = [
    "scrapers.chatyachalupy",
    "scrapers.e_chalupy",
    "scrapers.cs_chalupy",
    "scrapers.chata",
]


def main() -> None:
    failed = []
    for mod in SCRAPER_MODULES:
        logger.info("=== running %s ===", mod)
        try:
            runpy.run_module(mod, run_name="__main__")
        except Exception:
            logger.exception("%s failed - continuing with the other sites", mod)
            failed.append(mod)

    logger.info("=== running geocode ===")
    runpy.run_path("geocode.py", run_name="__main__")

    logger.info("=== running filter ===")
    runpy.run_path("filter.py", run_name="__main__")

    logger.info("=== exporting report data ===")
    runpy.run_path("export_report_data.py", run_name="__main__")

    if failed:
        logger.warning("finished with %d scraper(s) failed: %s", len(failed), failed)
        sys.exit(1)
    logger.info("done - see data/filtered.json")


if __name__ == "__main__":
    main()
