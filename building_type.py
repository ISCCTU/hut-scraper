"""Heuristic single-building vs. multi-unit classifier, from already-scraped
text - no extra requests. "One big hut" vs "a handful of small cabins" isn't
a field any of the four sites expose directly, but e-chalupy.cz's own listing
cards sometimes state "N objektu" outright, and Czech listing prose reliably
uses a small, distinct vocabulary for each case (plural "chatky"/"srubu"/
"apartmany" for scattered small units vs. singular "chalupa"/"chata"/"vila"
for one building) - good enough for a confidence signal, not a certainty.
"""
from __future__ import annotations

import re

_OBJEKT_RE = re.compile(r"(\d+)\s*objekt", re.IGNORECASE)
_MULTI_WORDS = [
    "chatky", "domky", "srubů", "roubenky", "kemp", "tábořiště",
    "glamping", "apartmány", "apartmánů", "hlavní budova",
]
_SINGLE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in
    [r"chalup", r"chat[aeů]", r"vil[ae]", r"roubenk", r"usedlost", r"statek",
     r"mlýn", r"zámeček", r"penzion", r"rodinný dům", r"stodol"]
]


def classify(rec: dict) -> tuple[str, str]:
    """Returns (kind, note): kind is 'single', 'multi', or 'unknown'."""
    name = (rec.get("name") or "").lower()
    desc = (rec.get("description_snippet") or "").lower()
    amenities = " ".join(rec.get("amenities") or []).lower()
    blob = f"{name} {desc} {amenities}"

    m = _OBJEKT_RE.search(blob)
    if m:
        n = int(m.group(1))
        return ("multi" if n >= 2 else "single"), f"{n} objekt{'y' if 1 < n < 5 else 'u' if n >= 5 else ''} (stated on listing)"

    hit = next((w for w in _MULTI_WORDS if w in blob), None)
    if hit:
        return "multi", f"mentions '{hit}'"

    hit_pattern = next((p for p in _SINGLE_PATTERNS if p.search(blob)), None)
    if hit_pattern:
        return "single", f"listed as '{hit_pattern.pattern.strip('[ae]').strip('ů')}'-type"

    return "unknown", "not enough text to tell"
