"""Seed page 3 ground-truth data into the database with Hindi + English.

Run inside the web container (has DB + transliterate module):

    docker compose exec -T web python -m scripts.seed_page3
"""
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import engine  # noqa: E402
from app.models import ElectoralRecord, ExtractionSession, ExtractionUnit, Document  # noqa: E402
from app.normalize import map_record  # noqa: E402
from app.transliterate import source_hash  # noqa: E402

GROUND_TRUTH = Path("/code/page3_results.json")

RELATION_MAP = {
    "पिता": "father",
    "पति": "husband",
    "माता": "mother",
    "अन्य": "other",
}


def safe_int(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def build_name(first, middle, last):
    parts = [p for p in [first, middle, last] if p and p.strip()]
    return " ".join(parts) if parts else ""


def build_relative_name(rec):
    """Build relative name based on relation type."""
    rel = rec.get("relation_name", "")
    if rel == "पिता":
        return build_name(
            rec.get("voter_father_first_name", ""),
            rec.get("voter_father_middle_name", ""),
            rec.get("voter_father_last_name", ""),
        )
    elif rel == "पति":
        return build_name(
            rec.get("voter_husband_first_name", ""),
            rec.get("voter_husband_middle_name", ""),
            rec.get("voter_husband_last_name", ""),
        )
    elif rel == "माता":
        return build_name(
            rec.get("voter_mother_first_name", ""),
            rec.get("voter_mother_middle_name", ""),
            rec.get("voter_mother_last_name", ""),
        )
    elif rel == "अन्य":
        return build_name(
            rec.get("voter_other_first_name", ""),
            rec.get("voter_other_middle_name", ""),
            rec.get("voter_other_last_name", ""),
        )
    return ""


def main() -> int:
    if not GROUND_TRUTH.exists():
        print(f"ground truth not found: {GROUND_TRUTH}")
        return 1

    data = json.loads(GROUND_TRUTH.read_text())
    records = data["records"] if isinstance(data, dict) else data
    print(f"Loaded {len(records)} records from page 3")

    doc_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee0")
    session_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1")
    unit_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee2")

    with Session(engine) as db:
        # Check if we already have this data
        existing = db.query(ElectoralRecord).filter(
            ElectoralRecord.document_id == doc_id
        ).count()
        if existing:
            print(f"Already {existing} records for seed-page3, skipping")
            return 0

        # Create the document record first (FK for session)
        doc = Document(
            id=doc_id,
            source_type="seed",
            source_document_id="seed-page3",
            source_path="/data/pdfs",
            file_name="2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf",
            file_size=1000000,
            file_hash="seed-hash-page3",
            source_modified_at=datetime.now(timezone.utc),
            discovered_at=datetime.now(timezone.utc),
            status="completed",
            attempt_count=1,
            max_attempts=3,
        )
        db.add(doc)

        # Create a fake session + unit to satisfy FKs
        session = ExtractionSession(
            id=session_id,
            document_id=doc_id,
            attempt_number=1,
            status="completed",
            pages_processed=3,
            pages_total=3,
            records_extracted=len(records),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        db.add(session)

        unit = ExtractionUnit(
            id=unit_id,
            session_id=session.id,
            unit_number=1,
            page_from=3,
            page_to=3,
            status="completed",
            attempt_count=1,
            records_extracted=len(records),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        db.add(unit)
        db.flush()

        # Map and insert each record
        inserted = 0
        for idx, rec in enumerate(records, 1):
            name_hi = build_name(
                rec.get("voter_first_name", ""),
                rec.get("voter_middle_name", ""),
                rec.get("voter_sur_name", ""),
            )
            rel_hi = build_relative_name(rec)
            rel_type = RELATION_MAP.get(rec.get("relation_name", ""), "other")

            item = {
                "source": {"page_number": 3, "row_number": idx},
                "hindi": {
                    "name": name_hi,
                    "relative_name": rel_hi,
                    "house_number": rec.get("house_no", "") or "",
                    "gender": rec.get("gender", "") or "",
                    "section_name": rec.get("anubhag_name", "") or "",
                },
                "english": {"name": None, "relative_name": None, "gender": None, "section_name": None},
                "common": {
                    "serial_number": idx,
                    "epic_number": rec.get("id_card_no", "") or "",
                    "age": safe_int(rec.get("age")),
                    "relationship_type": rel_type,
                },
                "confidence": 0.95,
            }

            mapped = map_record(item, str(doc_id), session.id, unit.id)
            db.add(mapped)
            inserted += 1

        db.commit()
        print(f"Inserted {inserted} records for page 3")
        print(f"  document_id: {doc_id}")
        print(f"  session_id: {session.id}")
        print(f"  unit_id:    {unit.id}")
        return 0


if __name__ == "__main__":
    sys.exit(main())