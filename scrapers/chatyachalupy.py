"""Scraper for chatyachalupy.cz.

Search is a real server-side-filtered endpoint (/parts/cs/filtered-places), and
availability for exact dates comes from a genuine JSON API on the detail page
(reserved-future-dates / disabled-dates) - no login needed. This is the
cleanest of the four sites: no guessing, no client-side-only filtering.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime

from bs4 import BeautifulSoup

from scrapers.common import Hut, MIN_CAPACITY, TRIP_CHECKIN, TRIP_CHECKOUT, build_session, polite_get, save_json

logger = logging.getLogger(__name__)

BASE = "https://www.chatyachalupy.cz"
NIGHT_1 = TRIP_CHECKIN.isoformat()  # "2026-11-13"
NIGHT_2 = (TRIP_CHECKIN.toordinal() + 1)
NIGHT_2 = datetime.fromordinal(NIGHT_2).date().isoformat()  # "2026-11-14"
TRIP_NIGHTS_ISO = {NIGHT_1, NIGHT_2}


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def fetch_candidate_cards(session) -> list[dict]:
    cards: list[dict] = []
    page = 1
    total = None
    while True:
        resp = polite_get(
            session,
            f"{BASE}/parts/cs/filtered-places",
            params={"capacity": MIN_CAPACITY, "capacityMode": "min", "page": page},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        if total is None:
            total_el = soup.select_one("#filter-total-value")
            if total_el and total_el.get("data-value"):
                total = int(total_el["data-value"])
                logger.info("filtered-places reports %d total results", total)

        items = soup.select("div.place-list-item-wrap")
        if not items:
            break

        for item in items:
            card = _parse_card(item)
            if card:
                cards.append(card)

        logger.info("page %d: %d cards (running total %d)", page, len(items), len(cards))
        if total is not None and len(cards) >= total:
            break
        page += 1
    return cards


def _parse_card(item) -> dict | None:
    link = item.select_one('a[href^="/nabidka-ubytovani/"]')
    if not link:
        return None
    href = link["href"]
    m = re.match(r"^/nabidka-ubytovani/([A-Za-z0-9]+)$", href)
    if not m:
        return None
    source_id = m.group(1)

    number_span = item.select_one(".place-item__number")
    name = ""
    if number_span:
        h3 = number_span.find_parent("h3")
        if h3:
            name = h3.get_text(" ", strip=True).replace(number_span.get_text(strip=True), "", 1).strip()

    capacity = None
    cap_el = item.select_one(".place-item__capacity")
    if cap_el:
        cap_m = re.search(r"(\d+)", cap_el.get_text(" ", strip=True))
        if cap_m:
            capacity = int(cap_m.group(1))

    region = ""
    region_el = item.select_one(".place-item__region")
    if region_el:
        region = region_el.get_text(" ", strip=True)

    price_from = None
    price_el = item.select_one(".place-item__price")
    if price_el:
        price_from = _parse_price(price_el.get_text())

    desc = ""
    text_block = item.select_one(".place-item__text p")
    if text_block:
        desc = text_block.get_text(" ", strip=True)[:300]

    return {
        "source_id": source_id,
        "url": BASE + href,
        "name": name,
        "capacity": capacity,
        "region": region,
        "price_from_czk": price_from,
        "description_snippet": desc,
    }


def enrich_detail(session, card: dict) -> Hut:
    hut = Hut(
        source="chatyachalupy.cz",
        source_id=card["source_id"],
        url=card["url"],
        name=card["name"],
        region=card["region"],
        capacity=card["capacity"],
        price_from_czk=card["price_from_czk"],
        price_basis="'from' price shown on listing card, unit (night/weekend/stay) unconfirmed",
        description_snippet=card["description_snippet"],
    )

    resp = polite_get(session, card["url"])
    if resp.status_code != 200:
        hut.availability_note = f"detail page returned HTTP {resp.status_code}"
        return hut
    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    place_id = None
    m = re.search(r'href="/rezervace\?place=(\d+)"', html)
    if m:
        place_id = m.group(1)

    map_div = soup.select_one("#place-map-data")
    if map_div and map_div.get("data-lon-lat-radius"):
        parts = map_div["data-lon-lat-radius"].split(";")
        if len(parts) >= 2:
            try:
                hut.lon = float(parts[0])
                hut.lat = float(parts[1])
            except ValueError:
                pass

    amenities = []
    for feat in soup.select(".place-content__favorite-feature"):
        img = feat.find("img")
        text = feat.get_text(" ", strip=True)
        if img:
            alt = img.get("alt", "")
            if alt and text.startswith(alt):
                text = text[len(alt):].strip()
        if text:
            amenities.append(text)
    hut.amenities = amenities

    bed_m = re.search(r"(\d+)\s*ložnic", html)
    if bed_m:
        hut.bedrooms = int(bed_m.group(1))

    region_link = soup.select_one('a[href^="/region/"]')
    if region_link:
        hut.address_text = region_link.get_text(" ", strip=True)

    if not place_id:
        hut.availability_note = "could not find place_id on detail page; availability unknown"
        return hut

    reserved = _fetch_json_list(session, f"{BASE}/api/place/{place_id}/reserved-future-dates", {"apartment": ""})
    disabled = _fetch_json_list(session, f"{BASE}/api/season/disabled-dates", {"placeId": place_id})

    if reserved is None or disabled is None:
        hut.availability_note = "availability API request failed"
        return hut

    def normalize(dates: list[str]) -> set[str]:
        out = set()
        for d in dates:
            out.add(d.replace("-single-day", ""))
        return out

    reserved_set = normalize(reserved)
    disabled_set = normalize(disabled)

    conflict_reserved = TRIP_NIGHTS_ISO & reserved_set
    conflict_disabled = TRIP_NIGHTS_ISO & disabled_set

    if conflict_reserved or conflict_disabled:
        hut.available_for_trip = False
        reasons = []
        if conflict_reserved:
            reasons.append(f"reserved: {sorted(conflict_reserved)}")
        if conflict_disabled:
            reasons.append(f"season-disabled: {sorted(conflict_disabled)}")
        hut.availability_note = "; ".join(reasons)
    else:
        hut.available_for_trip = True
        hut.availability_note = "no conflicts found in reserved-dates or disabled-dates APIs"

    prices = _fetch_json_dict(session, f"{BASE}/api/place/{place_id}/special-future-prices", {"apartment": ""})
    if prices and NIGHT_1 in prices and NIGHT_2 in prices:
        try:
            total = 0
            for night in (NIGHT_1, NIGHT_2):
                v = prices[night]
                if isinstance(v, dict):
                    v = v.get("price") or v.get("value")
                total += int(re.sub(r"[^\d]", "", str(v)))
            hut.price_for_trip_czk = total
        except (ValueError, TypeError):
            pass

    return hut


def _fetch_json_list(session, url: str, params: dict) -> list | None:
    try:
        resp = polite_get(session, url, params=params, delay=0.5)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning("failed fetching %s: %s", url, e)
        return None


def _fetch_json_dict(session, url: str, params: dict) -> dict | None:
    try:
        resp = polite_get(session, url, params=params, delay=0.5)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("failed fetching %s: %s", url, e)
        return None


def scrape() -> list[Hut]:
    session = build_session()
    cards = fetch_candidate_cards(session)
    logger.info("found %d candidate cards with capacity>=%d", len(cards), MIN_CAPACITY)

    huts = []
    for i, card in enumerate(cards, 1):
        logger.info("[%d/%d] enriching %s (%s)", i, len(cards), card["source_id"], card["name"])
        huts.append(enrich_detail(session, card))
    return huts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    huts = scrape()
    save_json(huts, "data/raw/chatyachalupy.json")

    available = [h for h in huts if h.available_for_trip is True]
    unknown = [h for h in huts if h.available_for_trip is None]
    unavailable = [h for h in huts if h.available_for_trip is False]
    print(f"\nTotal candidates (capacity>={MIN_CAPACITY}): {len(huts)}")
    print(f"Available for {TRIP_CHECKIN}..{TRIP_CHECKOUT}: {len(available)}")
    print(f"Unavailable: {len(unavailable)}")
    print(f"Unknown: {len(unknown)}")
