"""What the live counter would show at each moment of the real roll.

No session is running, so this takes the seven real ExtractionUnit rows from
the database, rewinds the clock to chosen moments, and asks live_progress()
what the page would render. Every duration, timestamp and record count is the
database's; only "now" is moved.

The point is the timeline. A batch counter sits still for ~70 seconds at a
time and then jumps. If this shows a number rising at every step, the thing
that was asked for is actually there.

Run inside the web container, which is the only place with DB access:
    docker compose cp tests/test_scripts/verify_live_counter.py web:/tmp/v.py
    docker compose exec -T web python /tmp/v.py
"""
import sys
from datetime import timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ExtractionSession, ExtractionUnit
from app.presentation import eta_summary, live_progress

SESSION_ID = "e49a1d5c-4f01-401c-aa7d-b4b89f06b097"


class Row:
    """A unit as the presentation layer sees it."""

    def __init__(self, u):
        self.unit_number = u.unit_number
        self.status = u.status
        self.page_from = u.page_from
        self.page_to = u.page_to
        self.records_extracted = u.records_extracted
        self.started_at = u.started_at
        self.completed_at = u.completed_at


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def main():
    with SessionLocal() as db:
        units = db.scalars(
            select(ExtractionUnit)
            .where(ExtractionUnit.session_id == SESSION_ID)
            .order_by(ExtractionUnit.unit_number)
        ).all()
        session = db.get(ExtractionSession, SESSION_ID)

    rows = [Row(u) for u in units]
    total_pages = session.pages_total
    truth = sum(r.records_extracted for r in rows)

    print(f"real units: {len(rows)}   pages {rows[0].page_from}-{rows[-1].page_to}"
          f"   pages_total={total_pages}   final records={truth}")
    print()
    print("what the page shows over the run:")
    print()

    t0 = min(_utc(r.started_at) for r in rows if r.started_at)

    def at(seconds):
        return t0 + timedelta(seconds=seconds)

    def show(label, when, mutate=None):
        snapshot = []
        for r in rows:
            copy = Row(r)
            if mutate:
                mutate(copy)
            snapshot.append(copy)
        p = live_progress(snapshot, pages_total=total_pages, at=when)

        if p["state"] == "done":
            headline = f"{p['records_settled']:,}"
            note = "records saved (finished)"
        elif p["can_estimate"] and p["records_estimate"] is not None:
            headline = f"~{p['records_estimate']:,}"
            more = max(0, p["records_estimate"] - p["records_settled"])
            note = f"{p['records_settled']:,} saved, ~{more:,} more being read"
        else:
            headline = f"{p['records_settled']:,}"
            note = ("reading the first batch - no pace to measure yet"
                    if p["state"] == "working" else "waiting to start")

        done = p["pages_estimate"] / (total_pages or 1)
        bar = "#" * int(round(done * 28))
        pct = f"{done * 100:3.0f}%"
        print(f"  +{int((when - t0).total_seconds()):>4}s  {label:<24}"
              f" {headline:>7}  [{bar:<28}] {pct}")
        print(f"                          {pct:>7}  {note}")
        eta = eta_summary(p["eta_seconds"])
        if eta and p["state"] != "done":
            print(f"                          {pct:>7}  {eta}")

    def pending_all(copy):
        copy.status = "pending"
        copy.started_at = None
        copy.completed_at = None
        copy.records_extracted = 0

    show("queued, nothing started", at(-1), pending_all)

    # Walk the real timeline: for each unit, show it mid-flight, then done.
    cursor = 0.0
    for idx, r in enumerate(rows):
        start = _utc(r.started_at)
        end = _utc(r.completed_at)
        if not start or not end:
            continue
        span = (end - start).total_seconds()
        mid = cursor + span * 0.5

        def flight(copy, _idx=idx, _start=start):
            if copy.unit_number == _idx + 1:
                copy.status = "processing"
                copy.completed_at = None
                copy.started_at = _start
            elif copy.unit_number > _idx + 1:
                pending_all(copy)

        show(f"batch {r.unit_number} halfway", at(mid), flight)
        cursor += span

    show("finished", at(cursor + 1))

    p = live_progress(rows, pages_total=total_pages, at=at(cursor + 1))
    print()
    print(f"reconciliation: counter says {p['records_settled']}, "
          f"database says {truth}")
    assert p["records_settled"] == truth, "counter disagrees with the database"
    assert p["state"] == "done"
    assert p["records_estimate"] is None, "a finished run must not project"
    print("OK: the counter lands on the real number and stops projecting.")


if __name__ == "__main__":
    main()