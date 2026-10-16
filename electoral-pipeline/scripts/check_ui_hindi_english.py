"""Seed one unit with page 3 ground-truth data to check the UI renders both languages.

Display-only verification. Reads the page 3 JSON, maps it through the same
extractor + normalize code the pipeline uses, and prints what the browser will
show. Writes nothing to the database.

    python -m scripts.check_ui_hindi_english
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.extractor import _join_name_parts, _join_relation_name, _relation_key  # noqa: E402
from app.normalize import map_record  # noqa: E402

GROUND_TRUTH = ROOT / "OCR/ground_truth_per_page_values_verified/json_files/page3_results.json"


def safe_int(value):
    """Same rule as app.extractor.safe_int: 0 must survive."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def main() -> int:
    if not GROUND_TRUTH.exists():
        print(f"ground truth not found: {GROUND_TRUTH}")
        return 1

    data = json.loads(GROUND_TRUTH.read_text())
    records = data["records"] if isinstance(data, dict) else data
    print(f"loaded {len(records)} records from page 3\n")

    shown = 0
    deva_leaks = []
    missing_english = []

    for idx, rec in enumerate(records[:10], 1):
        item = {
            "source": {"page_number": 3, "row_number": idx},
            "hindi": {
                "name": _join_name_parts(rec, "voter"),
                "relative_name": _join_relation_name(rec),
                "house_number": rec.get("house_no", ""),
                "gender": rec.get("gender", ""),
                "section_name": rec.get("anubhag_name", ""),
            },
            "english": {"name": None, "relative_name": None, "gender": None, "section_name": None},
            "common": {
                "serial_number": idx,
                "epic_number": rec.get("id_card_no", ""),
                "age": safe_int(rec.get("age")),
                "relationship_type": _relation_key(rec.get("relation_name")),
            },
            "confidence": 0.95,
        }

        mapped = map_record(item, document_id="ui-check", session_id="ui-check", unit_id="ui-check")

        hi = mapped.name_hi or "—"
        en = mapped.name_en or "—"
        rel_hi = mapped.relative_name_hi or "—"
        rel_en = mapped.relative_name_en or "—"

        print(f"row {idx:2}  page {mapped.page_number}")
        print(f"   name     HI: {hi}")
        print(f"            EN: {en}")
        print(f"   relative HI: {rel_hi}")
        print(f"            EN: {rel_en}")
        print(f"   gender    : {mapped.gender_hi or '—'} / {mapped.gender_en or '—'}")
        print(f"   house     : {mapped.house_number_hi or '—'}    age: {mapped.age}")
        print()

        # The check that matters: no Devanagari may reach an *_en column.
        for field, value in [
            ("name_en", mapped.name_en),
            ("relative_name_en", mapped.relative_name_en),
            ("gender_en", mapped.gender_en),
            ("section_name_en", mapped.section_name_en),
        ]:
            if value and any("ऀ" <= ch <= "ॿ" for ch in str(value)):
                deva_leaks.append(f"row {idx} {field}={value!r}")
        if mapped.name_hi and not mapped.name_en:
            missing_english.append(idx)
        shown += 1

    print(f"checked {shown} records")
    print(f"Hindi left in an English column : {len(deva_leaks)}")
    for leak in deva_leaks:
        print(f"   LEAK {leak}")
    print(f"records missing English name    : {len(missing_english)}")

    return 1 if deva_leaks else 0


if __name__ == "__main__":
    sys.exit(main())
