"""Idempotent schema + data migration for the bilingual columns.

Two jobs, both safe to run repeatedly:

1. Schema. `Base.metadata.create_all()` in app.init_db creates missing TABLES
   but never adds a COLUMN to a table that already exists. A deployment that
   predates the bilingual work therefore keeps its old shape no matter how
   often init runs. This adds any column the models declare that the database
   is missing.

2. Backfill. Rows written before this change are worse than empty in their
   *_en columns: app/extractor.py copied the Hindi text straight into them, so
   name_en literally held Devanagari. Any row whose English is missing, still
   Devanagari, or out of step with translit_source_hash is recomputed from the
   Hindi stored alongside it.

Run:
    python -m app.migrate                # apply
    python -m app.migrate --dry-run      # report only, change nothing
    python -m app.migrate --schema-only  # skip the row backfill

Known limitation, reported at the end: relationship_type on pre-existing rows
is NOT repaired. The extractor hardcoded "father" for every record, and the
Hindi relation type was never stored, so the correct value is not recoverable
from the database — only by re-extracting. Re-running a document's extraction
is the only way to fix those rows.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys

from sqlalchemy import inspect, text

from app.db import SessionLocal, engine
from app.models import ElectoralRecord  # noqa: F401  (registers metadata)
from app.transliterate import gender_en, transliterate

log = logging.getLogger("app.migrate")

# Devanagari block, used to detect the old "English column holding Hindi" rows.
# Square brackets are required: without them Postgres reads "ऀ-ॿ" as a literal
# alternation and matches nothing, which silently makes the backfill a no-op.
DEVA = "[ऀ-ॿ]"

# (column, python type) for every column this migration knows how to add.
# Kept explicit rather than derived so adding a column to models.py does not
# silently become a schema change in production.
KNOWN_COLUMNS: dict[str, tuple[str, dict]] = {
    "translit_source_hash": ("VARCHAR(64)", {"nullable": True}),
    "sno": ("INTEGER", {"nullable": True}),
    "card_index": ("INTEGER", {"nullable": True}),
    "voter_sr_no": ("VARCHAR(50)", {"nullable": True}),
    "is_deleted": ("BOOLEAN", {"nullable": False, "default": False}),
    "state_code": ("VARCHAR(10)", {"nullable": True}),
    "ac_code": ("VARCHAR(10)", {"nullable": True}),
    "anubhag_code": ("INTEGER", {"nullable": True}),
    "anubhag_name": ("TEXT", {"nullable": True}),
    "booth_code": ("VARCHAR(10)", {"nullable": True}),
    "pdf_name": ("TEXT", {"nullable": True}),
    "needs_review": ("BOOLEAN", {"nullable": False, "default": False}),
    "review_reasons": ("JSONB", {"nullable": True}),
    "field_sources": ("JSONB", {"nullable": True}),
    # Voter name components (Hindi + English)
    "voter_first_name_hi": ("TEXT", {"nullable": True}),
    "voter_first_name_en": ("TEXT", {"nullable": True}),
    "voter_middle_name_hi": ("TEXT", {"nullable": True}),
    "voter_middle_name_en": ("TEXT", {"nullable": True}),
    "voter_sur_name_hi": ("TEXT", {"nullable": True}),
    "voter_sur_name_en": ("TEXT", {"nullable": True}),
    # Relative name components (Hindi + English)
    "relative_name_hi": ("TEXT", {"nullable": True}),
    "relative_name_en": ("TEXT", {"nullable": True}),
    "relation_name": ("VARCHAR(50)", {"nullable": True}),
    # Father's name components
    "voter_father_first_name_hi": ("TEXT", {"nullable": True}),
    "voter_father_first_name_en": ("TEXT", {"nullable": True}),
    "voter_father_middle_name_hi": ("TEXT", {"nullable": True}),
    "voter_father_middle_name_en": ("TEXT", {"nullable": True}),
    "voter_father_last_name_hi": ("TEXT", {"nullable": True}),
    "voter_father_last_name_en": ("TEXT", {"nullable": True}),
    # Husband's name components
    "voter_husband_first_name_hi": ("TEXT", {"nullable": True}),
    "voter_husband_first_name_en": ("TEXT", {"nullable": True}),
    "voter_husband_middle_name_hi": ("TEXT", {"nullable": True}),
    "voter_husband_middle_name_en": ("TEXT", {"nullable": True}),
    "voter_husband_last_name_hi": ("TEXT", {"nullable": True}),
    "voter_husband_last_name_en": ("TEXT", {"nullable": True}),
    # Mother's name components
    "voter_mother_first_name_hi": ("TEXT", {"nullable": True}),
    "voter_mother_first_name_en": ("TEXT", {"nullable": True}),
    "voter_mother_middle_name_hi": ("TEXT", {"nullable": True}),
    "voter_mother_middle_name_en": ("TEXT", {"nullable": True}),
    "voter_mother_last_name_hi": ("TEXT", {"nullable": True}),
    "voter_mother_last_name_en": ("TEXT", {"nullable": True}),
    # Other relative's name components
    "voter_other_first_name_hi": ("TEXT", {"nullable": True}),
    "voter_other_first_name_en": ("TEXT", {"nullable": True}),
    "voter_other_middle_name_hi": ("TEXT", {"nullable": True}),
    "voter_other_middle_name_en": ("TEXT", {"nullable": True}),
    "voter_other_last_name_hi": ("TEXT", {"nullable": True}),
    "voter_other_last_name_en": ("TEXT", {"nullable": True}),
}

BACKFILL_PAIRS = [
    ("name_en", "name_hi", transliterate),
    ("relative_name_en", "relative_name_hi", transliterate),
    ("section_name_en", "section_name_hi", transliterate),
    ("house_number_en", "house_number_hi", transliterate),
    ("gender_en", "gender_hi", gender_en),
]

BATCH = 500


def ensure_columns() -> list[str]:
    """Add any declared column the table is missing. Returns what was added."""
    insp = inspect(engine)
    if "electoral_records" not in insp.get_table_names():
        log.info("table electoral_records absent; nothing to alter")
        return []
    existing = {c["name"] for c in insp.get_columns("electoral_records")}
    added = []
    with engine.begin() as conn:
        for name, (ddl, opts) in KNOWN_COLUMNS.items():
            if name in existing:
                continue
            null_sql = "" if opts.get("nullable", True) else " NOT NULL"
            conn.execute(text(
                f"ALTER TABLE electoral_records ADD COLUMN IF NOT EXISTS {name} {ddl}{null_sql}"
            ))
            added.append(name)
            log.info("added column electoral_records.%s %s", name, ddl)
    return added


def _needs_backfill(en_col: str, hi_col: str) -> str:
    """SQL predicate: English is absent, still Devanagari, or stale."""
    return text(
        f"({hi_col} IS NOT NULL AND {hi_col} <> '') AND ("
        f"  {en_col} IS NULL OR {en_col} = ''"
        f"  OR {en_col} ~ :deva"
        f")"
    )


def backfill_english(dry_run: bool = False) -> dict[str, int]:
    """Recompute English from stored Hindi. Returns per-column update counts."""
    counts: dict[str, int] = {}
    with SessionLocal() as db:
        for en_col, hi_col, fn in BACKFILL_PAIRS:
            rows = db.execute(
                text(f"SELECT id, {hi_col} AS hi FROM electoral_records "
                     f"WHERE {_needs_backfill(en_col, hi_col)} LIMIT :lim"),
                {"deva": DEVA, "lim": BATCH},
            ).fetchall()
            if not rows:
                counts[en_col] = 0
                continue
            updates = [
                {"id": r.id, "val": fn(r.hi)}
                for r in rows
                if r.hi
            ]
            if not dry_run and updates:
                db.execute(
                    text(f"UPDATE electoral_records SET {en_col} = :val WHERE id = :id"),
                    updates,
                )
                db.commit()
            counts[en_col] = len(updates)
            log.info("backfilled %s for %d row(s)", en_col, len(updates))
    return counts


def report_stale_relationship() -> int:
    """Count pre-existing rows whose relationship_type may be the old
    hardcoded 'father'. Advisory only -- the value is not repairable here."""
    with SessionLocal() as db:
        return db.execute(text(
            "SELECT count(*) FROM electoral_records "
            "WHERE relationship_type = 'father' AND relative_name_hi IS NOT NULL"
        )).scalar() or 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--schema-only", action="store_true", help="skip the backfill")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")

    added = [] if args.dry_run else ensure_columns()
    print(f"schema: {'no changes needed' if not added else 'added ' + ', '.join(added)}")

    if args.schema_only:
        return 0

    counts = backfill_english(dry_run=args.dry_run)
    total = sum(counts.values())
    verb = "would backfill" if args.dry_run else "backfilled"
    print(f"english: {verb} {total} value(s) — " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    if total:
        print(
            f"\nNOTE: only the first {BATCH} rows per column are handled per run. "
            "Re-run until every count is 0 for a large table."
        )

    suspect = report_stale_relationship()
    if suspect:
        print(
            f"\nWARNING: {suspect} row(s) have relationship_type='father'. Rows written "
            "before this change hardcoded that value and the Hindi relation type was "
            "never stored, so it cannot be repaired from the database — re-extract "
            "those documents."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
