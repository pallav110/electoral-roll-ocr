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


def missing_columns(table: str, known: dict[str, tuple[str, dict]]) -> list[str]:
    """Which declared columns `table` lacks. Read-only, so it is safe to call
    during a dry run -- which is the entire point of splitting this out.

    This used to live inside ensure_columns(), which main() simply did not
    call under --dry-run. The report therefore read "no changes needed" whether
    or not work was pending, because it was reporting that it had not looked.
    A dry run that reports health it never checked is worse than no dry run:
    it reads as a clean bill of health and stops the investigation.
    """
    insp = inspect(engine)
    if table not in insp.get_table_names():
        log.info("table %s absent; nothing to alter", table)
        return []
    existing = {c["name"] for c in insp.get_columns(table)}
    return [name for name in known if name not in existing]


def _describe(names: list[str], known: dict[str, tuple[str, dict]], verb: str) -> str:
    """One-line summary of a schema delta. `verb` is 'would add' under
    --dry-run and 'added' after a real run -- a report that says 'would' about
    something already done is its own small lie.

    Items may be a bare column name (looked up in `known`) or an already
    rendered "index <name>" string, which is how ensure_document_columns
    reports the indexes it created alongside its columns.
    """
    if not names:
        return "no changes needed"
    parts = [n if n.startswith("index ") else f"{n} {known[n][0]}" for n in names]
    return f"{verb} " + ", ".join(parts)


def _add_column_sql(table: str, name: str, ddl: str, opts: dict) -> str:
    """DDL for one ADD COLUMN, honouring every declared option.

    The DEFAULT was not emitted before. Two NOT NULL columns (is_deleted,
    needs_review) declare `"default": False`, and that default was silently
    dropped, so the statement became `is_deleted BOOLEAN NOT NULL` with no
    default -- which Postgres rejects on any table that already holds rows.
    The failure is invisible on an up-to-date database, where the columns
    already exist and no DDL runs at all; it only appears on exactly the
    older deployment this migration exists to rescue.

    A default is emitted for every column, not just the NOT NULL ones, so a
    future non-nullable column cannot reintroduce the same trap.
    """
    parts = [f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"]
    if "default" in opts:
        default = opts["default"]
        literal = ("true" if default is True else "false" if default is False
                   else str(default))
        parts.append(f"DEFAULT {literal}")
    if not opts.get("nullable", True):
        parts.append("NOT NULL")
    return " ".join(parts)


def ensure_columns() -> list[str]:
    """Add any declared column the table is missing. Returns what was added."""
    added = missing_columns("electoral_records", KNOWN_COLUMNS)
    if not added:
        return []
    with engine.begin() as conn:
        for name in added:
            ddl, opts = KNOWN_COLUMNS[name]
            conn.execute(text(
                _add_column_sql("electoral_records", name, ddl, opts)
            ))
            log.info("added column electoral_records.%s %s", name, ddl)
    return added


# (table, column, DDL, opts). The electoral_records columns above are kept
# separate because that table's backfill is driven by them; documents needs
# none, so it gets a plain additive column.
KNOWN_DOCUMENT_COLUMNS: dict[str, tuple[str, dict]] = {
    # Which scan root found this file. See Document.scan_root_id for why
    # this is stored rather than derived from source_path.
    "scan_root_id": ("INTEGER", {"nullable": True}),
}


# (constraint name, DDL). The FK is added separately from the column because
# ADD COLUMN ... REFERENCES is fine on a nullable column but cannot be
# combined with IF NOT EXISTS in older PostgreSQL, and this must be
# re-runnable against an already-migrated database.
#
# Only used when no FK on the column exists at all. A fresh create_all() emits
# documents_scan_root_id_fkey for the same relationship, which satisfies the
# reference without a second one -- see missing_document_constraints().
KNOWN_DOCUMENT_CONSTRAINTS: dict[str, str] = {
    "fk_documents_scan_root": (
        "FOREIGN KEY (scan_root_id) REFERENCES scan_roots(id)"
    ),
}


# Model-declared indexes on documents that create_all() builds but that an
# existing database predating them never received. create_all() only ever runs
# on an empty database, so an already-migrated deployment misses any index
# added to models.py later -- which is exactly how idx_documents_scan_root
# came to exist on fresh installs and nowhere else. Every click on a folder
# runs WHERE scan_root_id = ?, so this one has a query behind it.
#
# CREATE INDEX IF NOT EXISTS is idempotent, so no presence check is needed.
KNOWN_DOCUMENT_INDEXES: dict[str, str] = {
    "idx_documents_scan_root": "CREATE INDEX IF NOT EXISTS idx_documents_scan_root "
                               "ON documents (scan_root_id)",
}


def ensure_document_indexes() -> list[str]:
    """Create any model-declared index `documents` is missing. Returns the
    ones added."""
    insp = inspect(engine)
    if "documents" not in insp.get_table_names():
        return []
    with engine.connect() as conn:
        present = {
            r[0] for r in conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'documents'"
            ))
        }
        missing = [n for n in KNOWN_DOCUMENT_INDEXES if n not in present]
    if not missing:
        return []
    with engine.begin() as conn:
        for name in missing:
            conn.execute(text(KNOWN_DOCUMENT_INDEXES[name]))
            log.info("added index %s", name)
    return missing


def missing_document_indexes() -> list[str]:
    """Model-declared indexes on `documents` that are absent. Read-only."""
    insp = inspect(engine)
    if "documents" not in insp.get_table_names():
        return []
    with engine.connect() as conn:
        present = {
            r[0] for r in conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'documents'"
            ))
        }
    return [n for n in KNOWN_DOCUMENT_INDEXES if n not in present]


def missing_document_constraints() -> list[str]:
    """Named FK constraints on `documents` that are genuinely absent.

    Read-only.

    Presence is checked by COLUMN, not by constraint name. create_all() emits
    its own constraint for the same relationship -- `documents_scan_root_id_fkey`
    -- so on a fresh database the column already carries an equivalent FK by
    the time this runs. Matching on the name alone made the migration add a
    second, duplicate FK to the same column. It is harmless to Postgres today
    (both constraints point at the same target) but it means "run the
    migration" is not idempotent against a fresh install, and a duplicate
    constraint is the kind of thing that makes a later schema change fail
    with a duplicate-object error nobody can explain.

    pg_constraint carries the column list but lives in another schema;
    information_schema carries the name, so this matches on the name and then
    confirms the column via a join on key_column_usage.
    """
    with engine.connect() as conn:
        missing = []
        for name, ddl in KNOWN_DOCUMENT_CONSTRAINTS.items():
            column = _fk_column_from_ddl(ddl)
            present = conn.execute(text(
                "SELECT 1 FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                "WHERE tc.table_name = 'documents' "
                "AND tc.constraint_type = 'FOREIGN KEY' "
                "AND kcu.column_name = :column"
            ), {"column": column}).first()
            if not present:
                missing.append(name)
        return missing


def _fk_column_from_ddl(ddl: str) -> str:
    """The single column an `ADD CONSTRAINT` DDL references. One column only;
    these are all single-column FKs, and a multi-column composite key would
    need the full column list compared as a set rather than a single name."""
    inner = ddl[ddl.index("(") + 1: ddl.index(")")]
    return inner.split(",")[0].strip()


def ensure_document_columns() -> list[str]:
    """Add any declared column `documents` is missing. Additive and safe to
    re-run: every statement is ADD COLUMN IF NOT EXISTS, no row is touched,
    and the column is nullable so existing documents need no backfill.

    The FK constraint is checked on every run, not only when the column is
    new: on a second run the column already exists but the constraint may
    still be missing if the first run died between the two statements, and
    skipping the check would leave the reference permanently unconstrained.
    """
    added = missing_columns("documents", KNOWN_DOCUMENT_COLUMNS)
    with engine.begin() as conn:
        for name in added:
            ddl, opts = KNOWN_DOCUMENT_COLUMNS[name]
            conn.execute(text(_add_column_sql("documents", name, ddl, opts)))
            log.info("added column documents.%s %s", name, ddl)

        # Outside the column loop on purpose -- see the docstring.
        for name in missing_document_constraints():
            conn.execute(text(
                f"ALTER TABLE documents ADD CONSTRAINT {name} "
                f"{KNOWN_DOCUMENT_CONSTRAINTS[name]}"
            ))
            log.info("added constraint %s", name)

    # Indexes live outside the column work because they exist independently of
    # it: a database can hold the column and still predate the index, which is
    # the state the live deployment was in.
    idx_added = ensure_document_indexes()
    return added + [f"index {n}" for n in idx_added]


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
    hardcoded 'father'. Advisory only -- the value is not repairable here.

    relationship_type is not in KNOWN_COLUMNS, so a deployment predating it
    has no such column. This query used to raise, and because it runs last,
    the traceback came after the backfill had already committed: the data work
    succeeded and the command still exited non-zero with no summary printed.
    A read-only advisory must never be able to fail a run that did its work.
    """
    with SessionLocal() as db:
        present = db.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'electoral_records' AND column_name = 'relationship_type'"
        )).first()
        if not present:
            log.info("relationship_type absent; skipping the staleness advisory")
            return 0
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

    # The dry run must still LOOK. Both the column lists and the constraint
    # list are computed read-only; only the DDL is withheld. Reporting
    # "no changes needed" from an unexamined code path is how a pending
    # migration reads as an applied one.
    if args.dry_run:
        miss = missing_columns("electoral_records", KNOWN_COLUMNS)
        print(f"schema: {_describe(miss, KNOWN_COLUMNS, 'would add')}")

        doc_miss = missing_columns("documents", KNOWN_DOCUMENT_COLUMNS)
        print(f"documents: {_describe(doc_miss, KNOWN_DOCUMENT_COLUMNS, 'would add')}")

        miss_con = missing_document_constraints()
        if miss_con:
            print(f"  (this run would add {' '.join(miss_con)})")

        miss_idx = missing_document_indexes()
        if miss_idx:
            print(f"  indexes: would create {' '.join(miss_idx)}")
    else:
        added = ensure_columns()
        print(f"schema: {_describe(added, KNOWN_COLUMNS, 'added')}")

        doc_added = ensure_document_columns()
        print(f"documents: {_describe(doc_added, KNOWN_DOCUMENT_COLUMNS, 'added')}")

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
