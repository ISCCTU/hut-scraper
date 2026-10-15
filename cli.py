"""Single entrypoint for the whole pipeline. Bakes in every ordering constraint
that used to live only in docstrings/README prose:

  scrape (4 sites, independent)
    -> enrich (common-room, ratings, availability - all patch data/raw/*.json
       in place and must finish before geocode; reparse-prices is a one-time
       migration, NOT part of this default order - see --only below)
    -> geocode (rebuilds data/combined.json from data/raw/*.json, full overwrite)
    -> transit (patches data/combined.json in place; must always rerun after geocode)
    -> filter (data/combined.json -> data/filtered.json)
    -> export (data/filtered.json -> report-data.json)
"""
from __future__ import annotations

import argparse
import logging
import os
import runpy
import sys

from pipeline import config as _config  # noqa: F401 - import-resolves-cleanly smoke check
from pipeline import export_report_data, filter as filter_stage, geocode, transit
from enrichment import availability_cs_chalupy, common_room, ratings, reparse_cs_chalupy_prices

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SCRAPER_ORDER = ["chatyachalupy", "e_chalupy", "cs_chalupy", "chata"]
SCRAPER_MODULES = {k: f"scrapers.{k}" for k in SCRAPER_ORDER}

# reparse-prices is a ONE-TIME migration (it repaired records scraped before
# scrapers/cs_chalupy.py's own parse_price_table bug was fixed - new scrapes
# already get the correct price directly). It is NOT in the default order:
# including it here would re-fetch all ~546 cs-chalupy.cz pages on every
# `cli.py all` for no ongoing benefit. Run it explicitly via
# `cli.py enrich --only reparse-prices` if ever needed again (e.g. another
# upstream price-table mislabeling bug is found).
ENRICH_ORDER = ["common-room", "ratings", "availability"]
ENRICH_FUNCS = {
    "common-room": common_room.main,
    "ratings": ratings.main,
    "availability": availability_cs_chalupy.main,
    "reparse-prices": reparse_cs_chalupy_prices.main,
}

RAW_PATHS = [
    "data/raw/chatyachalupy.json", "data/raw/e_chalupy.json",
    "data/raw/cs_chalupy.json", "data/raw/chata.json",
]


def run_scrape(only: list[str] | None) -> list[str]:
    failed = []
    for key in (only or SCRAPER_ORDER):
        mod = SCRAPER_MODULES[key]
        logger.info("=== scrape: %s ===", mod)
        try:
            runpy.run_module(mod, run_name="__main__")
        except Exception:
            logger.exception("%s failed - continuing with the other sites", mod)
            failed.append(key)
    return failed


def run_enrich(only: list[str] | None) -> list[str]:
    failed = []
    for step in (only or ENRICH_ORDER):
        logger.info("=== enrich: %s ===", step)
        try:
            ENRICH_FUNCS[step]()
        except Exception:
            logger.exception("enrich step %r failed - continuing with remaining steps", step)
            failed.append(step)
    return failed


def run_geocode() -> bool:
    existing = [p for p in RAW_PATHS if os.path.exists(p)]
    missing = set(RAW_PATHS) - set(existing)
    if missing:
        logger.warning("skipping missing raw files (scraper not finished yet?): %s", sorted(missing))
    try:
        geocode.main(existing)
    except Exception:
        logger.exception("geocode failed")
        return False
    return True


def run_transit() -> bool:
    try:
        transit.main("data/combined.json")
    except Exception:
        logger.exception("transit failed")
        return False
    return True


def run_filter() -> bool:
    try:
        filter_stage.main()
    except Exception:
        logger.exception("filter failed")
        return False
    return True


def run_export(in_path: str = "data/filtered.json", out_path: str = "report-data.json") -> bool:
    try:
        export_report_data.main(in_path, out_path)
    except Exception:
        logger.exception("export failed")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cli.py")
    sub = p.add_subparsers(dest="stage", required=True)

    sp = sub.add_parser("scrape")
    sp.add_argument("--only", type=lambda s: s.split(","))
    sp = sub.add_parser("enrich")
    sp.add_argument("--only", type=lambda s: s.split(","))
    sub.add_parser("geocode")
    sub.add_parser("transit")
    sub.add_parser("filter")
    sp = sub.add_parser("export")
    sp.add_argument("in_path", nargs="?", default="data/filtered.json")
    sp.add_argument("out_path", nargs="?", default="report-data.json")
    sub.add_parser("fast")   # scrape -> geocode -> filter -> export (old main.py's scope)
    sub.add_parser("all")    # scrape -> enrich (default 3 steps) -> geocode -> transit -> filter -> export

    args = p.parse_args(argv)
    failed: list[str] = []

    if args.stage == "scrape":
        failed = run_scrape(args.only)
    elif args.stage == "enrich":
        failed = run_enrich(args.only)
    elif args.stage == "geocode":
        if not run_geocode():
            failed.append("geocode")
    elif args.stage == "transit":
        if not run_transit():
            failed.append("transit")
    elif args.stage == "filter":
        if not run_filter():
            failed.append("filter")
    elif args.stage == "export":
        if not run_export(args.in_path, args.out_path):
            failed.append("export")
    elif args.stage == "fast":
        failed += run_scrape(None)
        if not run_geocode():
            failed.append("geocode")
        if not run_filter():
            failed.append("filter")
        if not run_export():
            failed.append("export")
    elif args.stage == "all":
        failed += run_scrape(None)
        failed += run_enrich(None)
        if not run_geocode():
            failed.append("geocode")
        if not run_transit():
            failed.append("transit")
        if not run_filter():
            failed.append("filter")
        if not run_export():
            failed.append("export")

    if failed:
        logger.warning("finished with failures in: %s", failed)
        return 1
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
