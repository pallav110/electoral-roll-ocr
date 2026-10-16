"""Polling-station geocoding from cover-page metadata.

This is deliberately independent of voter OCR and of map-image extraction.
It calls the project's local India OSM/Nominatim instance, never a public
geocoder, and stores coordinates only when the returned address proves it is
inside the cover's declared state and district.  Everything else remains an
explicitly unverified text-search candidate rather than a convincing wrong pin.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.db import SessionLocal
from app.models import Document, DocumentCover, DocumentLocation
from app.transliterate import transliterate


_STATE_ALIASES = {
    "09": ("Uttar Pradesh", "उत्तर प्रदेश"),
    "24": ("Uttar Pradesh", "उत्तर प्रदेश"),  # legacy S24 roll convention
    "20": ("Rajasthan", "राजस्थान"),
    "29": ("Karnataka", "कर्नाटक"),
    "27": ("Maharashtra", "महाराष्ट्र"),
    "10": ("Bihar", "बिहार"),
    "18": ("Assam", "असम"),
    "21": ("Odisha", "ओडिशा", "उड़ीसा"),
    "26": ("Jharkhand", "झारखंड"),
}

# Helpful for lookup, but never enough to identify one particular station.
_GENERIC_STATION_WORDS = {
    "school", "vidyalaya", "college", "government", "govt", "primary", "junior", "high", "public", "panchayat", "bhawan", "bhavan", "hall", "centre", "center", "building", "office", "विद्यालय", "स्कूल", "कॉलेज", "राजकीय", "सरकारी", "प्राथमिक", "जूनियर", "उच्च", "पंचायत", "भवन", "केन्द्र", "केंद्र", "कार्यालय",
}


def _expand_station_abbreviations(value: str) -> str:
    """Expand unambiguous polling-station abbreviations for local search."""
    value = _clean(value)
    if not value:
        return ""
    for pattern, replacement in (
        (r"\b(?:govt\.?|gov\.?|gvt\.?)\b", "Government"),
        (r"\b(?:pri\.?|prim\.?)\b", "Primary"),
        (r"\b(?:sch\.?|scl\.?)\b", "School"),
        (r"\b(?:jhs)\b", "Junior High School"),
        (r"प्रा\s*[.0]?\s*वि\s*[.0]?", "प्राथमिक विद्यालय"),
        (r"रा\s*[.0]?\s*वि\s*[.0]?", "राजकीय विद्यालय"),
    ):
        value = re.sub(pattern, replacement, value, flags=re.I)
    # A regex word boundary intentionally leaves a trailing abbreviation dot
    # behind (the dot itself has no word boundary). It is punctuation, not a
    # name character, so remove it before composing an OSM query.
    value = re.sub(r"(?<=\w)\.(?=\s|$)", "", value)
    return _clean(value)


def _station_search_variants(value: str | None) -> list[str]:
    """Small deterministic set of search variants; never verification data."""
    original = _clean(value)
    expanded = _expand_station_abbreviations(original)
    variants = [original, expanded]
    if re.search(r"[\u0900-\u097f]", expanded):
        roman = _clean(transliterate(expanded))
        if roman:
            variants.append(roman)
    seen: set[str] = set()
    return [item for item in variants if item and not (item.casefold() in seen or seen.add(item.casefold()))]


def _station_distinctive_tokens(value: str | None) -> set[str]:
    generic = {_fold(word) for word in _GENERIC_STATION_WORDS}
    return {
        _fold(token) for token in re.findall(r"[A-Za-z\u0900-\u097F]{3,}", _clean(value))
        if _fold(token) and _fold(token) not in generic
    }


def _station_name_matches(cover: DocumentCover, candidate: dict) -> bool:
    """Require a full printed-name hit or a distinctive station-name token."""
    printed = _fold(cover.polling_station_name)
    candidate_text = _fold(_haystack(candidate))
    if not printed or not candidate_text:
        return False
    if printed in candidate_text:
        return True
    candidate_tokens = {
        _fold(token) for token in re.findall(r"[A-Za-z\u0900-\u097F]{3,}", _clean(_haystack(candidate)))
    }
    return bool(_station_distinctive_tokens(cover.polling_station_name) & candidate_tokens)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[\\/|]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ,;:-")
    return value


def _fold(value: str | None) -> str:
    return re.sub(r"[^\w]+", "", _clean(value).casefold(), flags=re.UNICODE)


def cover_location_query(cover: DocumentCover) -> str:
    """Create a context-rich search query without treating a part number as GPS."""
    aliases = _STATE_ALIASES.get((cover.state_code or "").strip(), ())
    state = _clean(cover.state_name)
    bits = [
        _clean(cover.polling_station_name),
        _clean(cover.polling_station_address),
        _clean(cover.ward),
        _clean(cover.village),
        _clean(cover.tehsil),
        _clean(cover.district),
        state,
        *aliases,
        "India",
    ]
    seen: set[str] = set()
    return ", ".join(bit for bit in bits if bit and not (bit.casefold() in seen or seen.add(bit.casefold())))


def _location_queries(cover: DocumentCover) -> list[str]:
    """Return progressively shorter, cover-only geocoder queries.

    A polling-station cover often has the official school name in one field
    and locality/district in separate fields.  Nominatim is much more likely
    to find a named school when it is not drowned out by every roll field.
    These are *alternative searches*, not fallbacks that invent a coordinate:
    every returned candidate still has to pass the same independent address
    checks below.
    """
    station_variants = _station_search_variants(cover.polling_station_name)
    station = station_variants[0] if station_variants else ""
    locality = [
        _clean(cover.polling_station_address), _clean(cover.ward),
        _clean(cover.village), _clean(cover.tehsil), _clean(cover.district),
    ]
    aliases = list(_STATE_ALIASES.get((cover.state_code or "").strip(), ()))
    state = _clean(cover.state_name)
    if state:
        aliases.insert(0, state)
    alternatives = [
        cover_location_query(cover),
        ", ".join(value for value in [station, *locality, *(aliases[:1]), "India"] if value),
        ", ".join(value for value in [station, _clean(cover.tehsil), _clean(cover.district), *(aliases[:1]), "India"] if value),
        ", ".join(value for value in [station, _clean(cover.district), *(aliases[:1]), "India"] if value),
    ]
    for variant in station_variants[1:]:
        alternatives.extend([
            ", ".join(value for value in [variant, *locality, *(aliases[:1]), "India"] if value),
            ", ".join(value for value in [variant, _clean(cover.tehsil), _clean(cover.district), *(aliases[:1]), "India"] if value),
        ])
    seen: set[str] = set()
    return [item for item in alternatives if item and not (item.casefold() in seen or seen.add(item.casefold()))]


def _haystack(candidate: dict) -> str:
    address = candidate.get("address") or {}
    return " ".join(str(value) for value in (candidate.get("display_name"), *address.values()) if value)


def _verified_score(cover: DocumentCover, candidate: dict) -> int:
    """Return a conservative evidence score.  Three signals are required."""
    address = candidate.get("address") or {}
    haystack = _haystack(candidate)
    folded = _fold(haystack)
    score = 0
    if str(address.get("country_code", "")).casefold() == "in" or "india" in folded:
        score += 1
    state_aliases = list(_STATE_ALIASES.get((cover.state_code or "").strip(), ()))
    if cover.state_name:
        state_aliases.append(cover.state_name)
    if any(_fold(alias) and _fold(alias) in folded for alias in state_aliases):
        score += 1
    # District is independent evidence; do not accept a matching school name
    # in another district merely because its state happens to match.
    if _fold(cover.district) and _fold(cover.district) in folded:
        score += 1
    # `\w` is deliberately Unicode-aware: many covers provide only the Hindi
    # polling-station name, while an OSM record can also be Hindi-only.
    if _station_name_matches(cover, candidate):
        score += 1
    return score


def _is_verified_candidate(cover: DocumentCover, candidate: dict) -> bool:
    """Require all geographic context *and* a station-name signal.

    Country/state/district alone can identify a district centroid.  That is
    useful as a search result but never sufficient to label a polling station
    or a map pin as exact.
    """
    if _verified_score(cover, candidate) < 4:
        return False
    return _station_name_matches(cover, candidate)


def _candidate_key(candidate: dict) -> tuple:
    return (candidate.get("osm_type"), candidate.get("osm_id"), candidate.get("lat"), candidate.get("lon"))


def _match_payload(candidate: dict) -> dict:
    address = candidate.get("address") or {}
    return {
        "osm_id": candidate.get("osm_id"), "osm_type": candidate.get("osm_type"),
        "class": candidate.get("class"), "type": candidate.get("type"),
        "display_name": candidate.get("display_name"), "address": address,
    }


def geocoder_ready() -> bool:
    """True only after the local Nominatim HTTP service is serving searches."""
    try:
        response = httpx.get(
            f"{config.LOCATION_GEOCODER_URL.rstrip('/')}/status",
            timeout=min(5, config.LOCATION_GEOCODER_TIMEOUT_SECONDS),
        )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def resolve_document_location(db: Session, document_id: str) -> dict:
    document = db.get(Document, document_id)
    if not document:
        return {"status": "missing"}
    cover = db.scalar(select(DocumentCover).where(DocumentCover.document_id == document.id))
    row = db.scalar(select(DocumentLocation).where(DocumentLocation.document_id == document.id))
    if row is None:
        row = DocumentLocation(document_id=document.id)
        db.add(row); db.flush()
    if not cover or cover.status != "completed":
        row.status, row.error = "pending", "Cover metadata is not available yet."
        db.commit(); return {"status": "pending"}
    queries = _location_queries(cover)
    query = queries[0] if queries else ""
    row.query, row.status, row.error = query or None, "processing", None
    db.commit()
    if not query:
        row.status, row.error, row.completed_at = "skipped", "No polling-station location text was read from the cover.", _now()
        db.commit(); return {"status": "skipped"}
    try:
        matches: list[dict] = []
        seen: set[tuple] = set()
        for candidate_query in queries:
            response = httpx.get(
                f"{config.LOCATION_GEOCODER_URL.rstrip('/')}/search",
                params={"q": candidate_query, "format": "jsonv2", "addressdetails": 1, "limit": 5, "countrycodes": "in"},
                timeout=config.LOCATION_GEOCODER_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            for candidate in response.json():
                key = _candidate_key(candidate)
                if key not in seen:
                    seen.add(key)
                    matches.append(candidate)
    except httpx.HTTPError as exc:
        # The importer can take hours. Keep the row pending, so a later sweep
        # automatically resumes instead of recording hundreds of false failures.
        row.status, row.error = "pending", f"Local geocoder is not ready: {exc}"
        db.commit(); return {"status": "pending", "reason": "geocoder_unavailable"}
    # Only count a real search. An India import can take hours; consuming the
    # retry budget while its HTTP service is unavailable would strand every
    # document before a single lookup was actually made.
    row.attempt_count = (row.attempt_count or 0) + 1
    if not matches:
        row.status, row.error, row.completed_at = "unverified", "No India OSM candidate matched the cover query.", _now()
        db.commit(); return {"status": "unverified"}
    ranked = sorted(matches, key=lambda item: _verified_score(cover, item), reverse=True)
    best, score = ranked[0], _verified_score(cover, ranked[0])
    row.matched_name = best.get("name") or best.get("display_name")
    row.matched_address = best.get("display_name")
    row.provider_payload, row.verification_score = _match_payload(best), score
    row.completed_at = _now()
    if _is_verified_candidate(cover, best):
        row.status, row.latitude, row.longitude = "verified", best.get("lat"), best.get("lon")
        row.error = None
    else:
        # Preserve the candidate for audit, but never expose its coordinates as
        # an exact polling-station location.
        row.status, row.latitude, row.longitude = "unverified", None, None
        row.error = "Candidate did not independently match country, state, and district."
    db.commit()
    return {"status": row.status, "score": score, "query": query}


def location_for_document(db: Session, document_id) -> DocumentLocation | None:
    return db.scalar(select(DocumentLocation).where(DocumentLocation.document_id == document_id))
