"""Replace queue-inclusive session durations with actual OCR execution time.

Idempotent and safe to run at every deployment.  Sessions without any
completed, timed unit are left untouched because no honest OCR duration can be
derived for them.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402


def main() -> int:
    statement = text("""
        WITH measured AS (
            SELECT
                session_id,
                floor(sum(extract(epoch FROM (completed_at - started_at))) * 1000)::bigint
                    AS processing_time_ms
            FROM extraction_units
            WHERE status = 'completed'
              AND started_at IS NOT NULL
              AND completed_at IS NOT NULL
              AND completed_at >= started_at
            GROUP BY session_id
        )
        UPDATE extraction_sessions AS session
        SET processing_time_ms = measured.processing_time_ms
        FROM measured
        WHERE session.id = measured.session_id
          AND session.processing_time_ms IS DISTINCT FROM measured.processing_time_ms
        RETURNING session.id
    """)
    with SessionLocal.begin() as db:
        changed = len(db.execute(statement).all())
    print(f"session durations: corrected {changed} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
