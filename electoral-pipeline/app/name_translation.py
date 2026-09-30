"""Name-specific Hindi→English transliteration improvements.

This module provides deterministic, offline name romanization for common
Indian proper names. It uses a curated mapping of known Hindi names to
their common English spellings, supplementing the mathematical IAST
transliteration.

Why not machine translation:
----------------------------
Neural translation models (Argos, Google Translate, etc.) perform
*semantic* translation, not name romanization:
- आकाश → "Sky" (meaning), not "Aakash" (name)
- बबीता → "Blessing", not "Babita"
- पिता → "daddy", not "Father"

For an electoral database, we need deterministic name spellings that
match how names appear on official English-language electoral rolls.

Approach:
---------
1. Curated mapping of common Indian names (loaded from JSON file)
2. Falls back to IAST transliteration for unknown names
3. Fully deterministic, offline, no API calls
4. Cache-friendly: JSON file serves as the cache
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Path to the curated name mappings JSON file
NAME_MAP_PATH = Path(os.getenv(
    "NAME_TRANSLATION_MAP_PATH",
    "./name_translation_map.json"
))

_NAME_MAP = None


def _load_name_map() -> dict:
    """Load the curated Hindi→English name mapping from JSON."""
    global _NAME_MAP
    if _NAME_MAP is not None:
        return _NAME_MAP

    if NAME_MAP_PATH.exists():
        try:
            _NAME_MAP = json.loads(NAME_MAP_PATH.read_text(encoding='utf-8'))
            log.info(f"Loaded {len(_NAME_MAP)} name mappings from {NAME_MAP_PATH}")
            return _NAME_MAP
        except Exception as e:
            log.warning(f"Failed to load name map: {e}")

    _NAME_MAP = {}
    return _NAME_MAP


def _save_name_map(name_map: dict):
    """Save the name mapping to JSON (for building the map)."""
    try:
        NAME_MAP_PATH.write_text(
            json.dumps(name_map, ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8'
        )
        log.info(f"Saved {len(name_map)} name mappings to {NAME_MAP_PATH}")
    except Exception as e:
        log.warning(f"Failed to save name map: {e}")


def translate_name(hindi_name: str) -> Optional[str]:
    """Get common English spelling for a Hindi proper name.

    Checks the curated name map first, then falls back to IAST
    transliteration (handled by the caller).

    Args:
        hindi_name: The Hindi name (e.g., "आकाश", "कुवरपाल टट")

    Returns:
        Common English spelling if in map, else None (caller falls back)
    """
    if not hindi_name or not hindi_name.strip():
        return None

    hindi_name = hindi_name.strip()
    name_map = _load_name_map()

    # Direct lookup
    if hindi_name in name_map:
        log.debug(f"Name map hit: {hindi_name} → {name_map[hindi_name]}")
        return name_map[hindi_name]

    # Try component-wise for multi-word names
    parts = hindi_name.split()
    if len(parts) > 1:
        translated_parts = []
        all_found = True
        for part in parts:
            if part in name_map:
                translated_parts.append(name_map[part])
            else:
                all_found = False
                break
        if all_found:
            result = " ".join(translated_parts)
            log.debug(f"Name map component hit: {hindi_name} → {result}")
            return result

    return None


def add_name_mapping(hindi: str, english: str):
    """Add a name mapping (for building the map interactively)."""
    name_map = _load_name_map()
    name_map[hindi] = english
    _save_name_map(name_map)
    log.info(f"Added name mapping: {hindi} → {english}")


def build_name_map_from_records(records: list[dict]):
    """Build name map from a list of records with known Hindi/English pairs.

    Args:
        records: List of dicts with 'hindi' and 'english' keys
    """
    name_map = _load_name_map()
    added = 0
    for rec in records:
        hi = rec.get('hindi', '').strip()
        en = rec.get('english', '').strip()
        if hi and en and hi not in name_map:
            name_map[hi] = en
            added += 1
    if added:
        _save_name_map(name_map)
    log.info(f"Built name map: {added} new entries added, {len(name_map)} total")