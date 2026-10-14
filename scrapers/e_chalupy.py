"""Scraper for e-chalupy.cz.

No usable capacity-filter query param exists (the persons picker is a client-side
React widget), so this walks every region's paginated listing (server-rendered,
~120 cards/page) and filters capacity>=20 in Python. Region pages already carry
capacity/bedrooms/short tags per card, so only capacity>=20 candidates ever need
a detail-page fetch (for exact GPS, address, season price breakdown and the
live date-availability check).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from scrapers.common import (
    BUDGET_MAX_CZK,
    MIN_CAPACITY,
    PREFERRED_CAPACITY,
    TRIP_CHECKIN,
    TRIP_CHECKOUT,
    Hut,
    build_session,
    polite_get,
    save_json,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

BASE = "https://www.e-chalupy.cz"

REGIONS = [
    "beskydy", "ceske-stredohori", "ceske-svycarsko", "cesky-raj", "jeseniky",
    "jizerske-hory", "jizni-cechy", "jizni-morava", "kokorinsko", "krkonose",
    "krusne-hory", "luzicke-hory", "okoli-prahy", "orlicke-hory",
    "severni-morava-a-slezsko", "stredni-cechy", "sumava", "vychodni-cechy",
    "vysocina",
]

MAX_PAGES_PER_REGION = 150
CAPACITY_RE = re.compile(r"(\d+)\s*osob", re.IGNORECASE)
BEDROOM_RE = re.compile(r"(\d+)\s*ložnic", re.IGNORECASE)
PROPERTY_ID_RE = re.compile(r"-o(\d+)$")
RESERVATIONS_RE = re.compile(r"window\.propertyReservations\s*=\s*(\[.*?\]);", re.DOTALL)
PRICE_INT_RE = re.compile(r"(\d[\d\s. ]*\d|\d)\s*Kč")


def parse_price_czk(text: str) -> int | None:
    """Parse a Kc amount, handling both space/nbsp and '.' as thousands separators
    ("24.000 Kc" and "24 000 Kc" both mean 24000) - a plain \\d[\\d\\s]* regex stops
    at the '.', backtracks past it, and silently matches just the last 3 digits as
    the whole price (e.g. "od 24.000 Kc" -> 0), which is wrong, not merely imprecise."""
    m = PRICE_INT_RE.search(text)
    if not m:
        return None
    digits = m.group(1).replace("\xa0", "").replace(" ", "").replace(".", "")
    return int(digits) if digits.isdigit() else None


def collect_region_candidates(session, region: str) -> list[dict]:
    """Walk every page of one region, return raw card dicts with capacity>=MIN_CAPACITY."""
    candidates = []
    page = 1
    while page <= MAX_PAGES_PER_REGION:
        resp = polite_get(session, f"{BASE}/{region}", params={"p": page}, delay=1.2)
        if resp.status_code != 200:
            logger.warning("region %s page %d: HTTP %d, stopping", region, page, resp.status_code)
            break
        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.select(".item.c-property")
        if not cards:
            break

        page_candidates = 0
        for card in cards:
            title_a = card.select_one("a.title")
            if not title_a or not title_a.get("href"):
                continue
            url = title_a["href"]
            name = title_a.get_text(strip=True)

            fav_btn = card.select_one("button.favourite[data-id]")
            source_id = fav_btn["data-id"] if fav_btn else None
            if not source_id:
                m = PROPERTY_ID_RE.search(url.split("?")[0])
                source_id = m.group(1) if m else None
            if not source_id:
                continue

            region_link = card.select_one("a.location2")
            region_name = region_link.get_text(strip=True) if region_link else region

            tag_texts = [t.get_text(strip=True) for t in card.select(".tags .tag.tag-sm")]
            capacity = None
            bedrooms = None
            amenity_tags = []
            for t in tag_texts:
                cm = CAPACITY_RE.search(t)
                bm = BEDROOM_RE.search(t)
                if cm:
                    capacity = int(cm.group(1))
                elif bm:
                    bedrooms = int(bm.group(1))
                else:
                    amenity_tags.append(t)

            if capacity is None or capacity < MIN_CAPACITY:
                continue

            price_el = card.select_one(".bottom .price")
            price_from = None
            price_basis = ""
            if price_el:
                unit_el = price_el.select_one(".l")
                unit_text = unit_el.get_text(strip=True) if unit_el else ""
                price_from = parse_price_czk(price_el.get_text(" ", strip=True))
                price_basis = f"listing-card 'from' price: {unit_text}" if unit_text else "listing-card 'from' price, unit unclear"

            desc_el = card.select_one(".desc")
            desc_snippet = ""
            if desc_el:
                desc_snippet = desc_el.get_text(" ", strip=True)
                desc_snippet = re.sub(r"\s*Více\s*$", "", desc_snippet).strip()[:300]

            page_candidates += 1
            candidates.append({
                "source_id": str(source_id),
                "url": url,
                "name": name,
                "region": region_name,
                "capacity": capacity,
                "bedrooms": bedrooms,
                "amenity_tags": amenity_tags,
                "price_from_czk": price_from,
                "price_basis": price_basis,
                "description_snippet": desc_snippet,
            })

        logger.info("region %s page %d: %d cards, %d candidates this page", region, page, len(cards), page_candidates)
        page += 1
    return candidates


def enrich_detail(session, cand: dict) -> Hut:
    url = cand["url"]
    resp = polite_get(session, url, delay=1.2)
    hut = Hut(
        source="e-chalupy.cz",
        source_id=cand["source_id"],
        url=url,
        name=cand["name"],
        region=cand["region"],
        capacity=cand["capacity"],
        bedrooms=cand["bedrooms"],
        price_from_czk=cand["price_from_czk"],
        price_basis=cand["price_basis"],
        amenities=cand["amenity_tags"],
        description_snippet=cand["description_snippet"],
    )
    if resp.status_code != 200:
        hut.availability_note = f"detail page fetch failed: HTTP {resp.status_code}"
        return hut

    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    lat_meta = soup.find("meta", attrs={"property": "place:location:latitude"})
    lon_meta = soup.find("meta", attrs={"property": "place:location:longitude"})
    if lat_meta and lat_meta.get("content"):
        try:
            hut.lat = float(lat_meta["content"])
        except ValueError:
            pass
    if lon_meta and lon_meta.get("content"):
        try:
            hut.lon = float(lon_meta["content"])
        except ValueError:
            pass

    ld_script = soup.find("script", attrs={"type": "application/ld+json"})
    if ld_script and ld_script.string:
        try:
            data = json.loads(ld_script.string)
            addr = data.get("address", {})
            parts = [addr.get("streetAddress", ""), addr.get("addressLocality", ""), addr.get("addressRegion", "")]
            hut.address_text = ", ".join(p for p in parts if p)
        except (json.JSONDecodeError, AttributeError):
            pass

    equip_section = soup.select_one("#vybaveni")
    if equip_section:
        equip_text = equip_section.get_text(" ", strip=True)
        keywords = ["bazén", "sauna", "vířivka", "whirlpool", "krb", "gril", "wifi", "wellness", "infrasauna"]
        found = sorted({kw for kw in keywords if kw.lower() in equip_text.lower()})
        hut.amenities = sorted(set(hut.amenities) | set(found))
        if not hut.description_snippet:
            hut.description_snippet = equip_text[:300]

    price_groups = soup.select_one("[class*=price-groups]")
    # keyed by (period, scope) -> list[(price, entry_text)]; scope distinguishes a whole-object
    # price from a per-apartment one, since for a multi-unit property those are NOT interchangeable
    # (booking everyone in means paying for every apartment, not just the cheapest one).
    buckets: dict[tuple[str, str], list[tuple[int, str]]] = {}
    if price_groups:
        for group in price_groups.select(".price-group"):
            title_el = group.select_one(".group-title")
            title = title_el.get_text(strip=True) if title_el else "?"
            for item in group.select(".price-item"):
                price_b = item.select_one(".price b")
                unit_i = item.select_one(".price i")
                if not price_b or not unit_i:
                    continue
                price_text = price_b.get_text(strip=True)
                unit_text = unit_i.get_text(strip=True)
                price_val = parse_price_czk(price_text)
                if price_val is None:
                    continue
                entry = f"{title}={price_text} ({unit_text})"
                period = "víkend" if "víkend" in unit_text.lower() else "týden" if "týden" in unit_text.lower() else None
                if period is None:
                    continue
                scope = "objekt" if "objekt" in unit_text.lower() else "apartmán" if "apartmán" in unit_text.lower() else "jiné"
                buckets.setdefault((period, scope), []).append((price_val, entry))

    def cheapest(period: str, scope: str) -> tuple[int, str] | None:
        vals = buckets.get((period, scope))
        return min(vals) if vals else None

    chosen = cheapest("víkend", "objekt") or cheapest("víkend", "jiné")
    basis_note = "exact Nov 13-15 rate unconfirmed"
    if chosen is None:
        chosen = cheapest("týden", "objekt") or cheapest("týden", "jiné")
        basis_note = "no weekend rate found; this is a WEEKLY rate, not a weekend price"
    if chosen is None and cheapest("víkend", "apartmán"):
        chosen = cheapest("víkend", "apartmán")
        basis_note = "only a PER-APARTMENT weekend rate found, not whole-object - multiply by apartments needed for full capacity"

    if chosen is not None:
        hut.price_from_czk = chosen[0]
        all_entries = [e for vals in buckets.values() for _, e in vals]
        hut.price_basis = f"{basis_note}; all season/unit rates seen: " + "; ".join(all_entries)

    # `POST .../demands/validate` looked like an availability check (it takes a date
    # range and returns {"status": true/false}) but it is NOT one - verified live
    # 2026-09-27: it returns status:true for a property with a confirmed overlapping
    # reservation. It almost certainly just validates that the date range is a
    # well-formed *inquiry* (this site is contact-the-owner, not instant-book), and
    # was never checked against a known-booked date range before being trusted.
    # The real calendar is `window.propertyReservations` - a plain JSON array of
    # {from, to, unitId} - embedded directly in every detail page's HTML.
    m = RESERVATIONS_RE.search(html)
    if not m:
        hut.availability_note = "window.propertyReservations not found on page"
        return hut
    try:
        reservations = json.loads(m.group(1))
    except json.JSONDecodeError:
        hut.availability_note = "window.propertyReservations found but failed to parse as JSON"
        return hut

    blocking = None
    for r in reservations:
        try:
            r_from, r_to = date.fromisoformat(r["from"]), date.fromisoformat(r["to"])
        except (KeyError, ValueError, TypeError):
            continue
        if r_from < TRIP_CHECKOUT and r_to > TRIP_CHECKIN:  # half-open interval overlap
            blocking = r
            break

    if blocking:
        hut.available_for_trip = False
        hut.availability_note = f"reserved {blocking['from']}..{blocking['to']} per window.propertyReservations on the page"
    else:
        hut.available_for_trip = True
        hut.availability_note = "no overlapping entry in window.propertyReservations on the page"

    return hut


# One-off personal scrape, time-boxed: this site alone has 2000+ candidates at capacity>=20,
# far more than the other three sites combined, so detail-page enrichment is capped and
# prioritized by closeness to PREFERRED_CAPACITY (covers the practically relevant ~20-45
# range) rather than crawled exhaustively or skewed toward the largest (mostly hotel-like)
# properties. Resumable: candidates already present in the output file from a prior run
# are skipped rather than re-fetched, so an interrupted run can just be rerun.
MAX_DETAIL_VISITS = 1000
CHECKPOINT_EVERY = 25
OUTPUT_PATH = "data/raw/e_chalupy.json"
_HUT_FIELDS = {f.name for f in dataclasses.fields(Hut)}


def _load_existing() -> dict[str, Hut]:
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    # Filter to known Hut fields - an enrichment script may have patched extra keys
    # (e.g. rating_score) directly onto this raw JSON file, and Hut(**d) must not
    # crash on a field it doesn't declare.
    return {
        d["source_id"]: Hut(**{k: v for k, v in d.items() if k in _HUT_FIELDS})
        for d in data
    }


def main() -> None:
    session = build_session()
    all_candidates: dict[str, dict] = {}
    for region in REGIONS:
        for cand in collect_region_candidates(region=region, session=session):
            all_candidates.setdefault(cand["source_id"], cand)

    existing = _load_existing()
    logger.info("%d candidates already fetched in a prior run, will be kept as-is", len(existing))

    # Range-aware AND stratified by exact capacity - two distinct failure modes, both
    # observed live, needed fixing together. (1) Pure distance-to-30 starves the whole
    # [MIN_CAPACITY, PREFERRED_CAPACITY] range once anything above PREFERRED_CAPACITY is
    # numerically closer (a new 33-capacity candidate beat an unfetched 19-capacity one
    # every time). Fixed by a hard in-range-first tier. (2) Even within that tier, distance
    # -to-30 as the tiebreak clusters the whole budget onto capacities right next to 30
    # (e.g. 24-27) and never reaches the far end (17-19) if there happen to be hundreds of
    # candidates at 24-27 alone - same starvation, just bounded. Fixed by round-robin: walk
    # capacity values 17,18,19,...,30,17,18,... one candidate at a time so every value in
    # the requested range gets a fair share of the budget instead of whichever value has
    # the deepest well of un-fetched listings winning it all.
    remaining_by_source = {c["source_id"]: c for c in all_candidates.values() if c["source_id"] not in existing}
    in_range = sorted((c for c in remaining_by_source.values() if MIN_CAPACITY <= c["capacity"] <= PREFERRED_CAPACITY),
                       key=lambda c: c["capacity"])
    out_of_range = sorted((c for c in remaining_by_source.values() if not (MIN_CAPACITY <= c["capacity"] <= PREFERRED_CAPACITY)),
                           key=lambda c: abs(c["capacity"] - PREFERRED_CAPACITY))

    by_capacity: dict[int, list[dict]] = {}
    for c in in_range:
        by_capacity.setdefault(c["capacity"], []).append(c)
    round_robin: list[dict] = []
    capacities = sorted(by_capacity)
    while any(by_capacity.values()):
        for cap in capacities:
            bucket = by_capacity.get(cap)
            if bucket:
                round_robin.append(bucket.pop(0))

    remaining = round_robin + out_of_range
    to_visit = remaining[: max(0, MAX_DETAIL_VISITS - len(existing))]
    skipped = remaining[len(to_visit):]

    logger.info(
        "total unique capacity>=%d candidates: %d (%d already fetched); visiting %d more, "
        "stratified across capacities %s, skipping %d",
        MIN_CAPACITY, len(all_candidates), len(existing), len(to_visit),
        sorted({c["capacity"] for c in to_visit}), len(skipped),
    )

    huts: list[Hut] = list(existing.values())
    for i, cand in enumerate(to_visit, 1):
        logger.info("[%d/%d] fetching detail for %s (%s, capacity %d)", i, len(to_visit), cand["name"], cand["source_id"], cand["capacity"])
        try:
            huts.append(enrich_detail(session, cand))
        except requests.exceptions.RequestException as e:
            logger.warning("failed to enrich %s: %s", cand["source_id"], e)
        if i % CHECKPOINT_EVERY == 0:
            save_json(huts, OUTPUT_PATH)

    save_json(huts, OUTPUT_PATH)

    meta = {
        f"total_candidates_found_capacity_ge_{MIN_CAPACITY}": len(all_candidates),
        "detail_fetched": len(huts),
        "not_visited_time_boxed": len(skipped),
        "prioritization": f"sorted by closeness to PREFERRED_CAPACITY={PREFERRED_CAPACITY} before capping at {MAX_DETAIL_VISITS} total",
    }
    with open("data/raw/e_chalupy.meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    available = sum(1 for h in huts if h.available_for_trip is True)
    within_budget = sum(1 for h in huts if h.price_from_czk is not None and h.price_from_czk <= BUDGET_MAX_CZK)
    logger.info("SUMMARY: %d total visited (%d newly fetched this run), %d available for trip dates, %d with price_from<=%d Kc",
                len(huts), len(to_visit), available, within_budget, BUDGET_MAX_CZK)


if __name__ == "__main__":
    main()
