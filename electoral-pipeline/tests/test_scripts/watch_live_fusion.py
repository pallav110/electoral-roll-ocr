"""Watch the fused counter move while a real extraction runs.

Everything else about this feature is tested against fakes. This is the one
script that uses the real database, the real worker and the real OCR service,
because the property that matters -- the headline climbing card by card
instead of jumping by 90 -- only exists when all three are running at once.

It re-runs one finished unit through `app.tasks.process_unit` -- the same
Celery task the beat scheduler dispatches, so the code path is the production
one. The UI's retry button cannot be used here: `retry_unit()` only accepts a
*failed* unit inside a *failed/partial* session, and the sessions in this
database are all `completed`, so it correctly answers 409.

Then it polls the status endpoint and prints what the session page would show
at each moment.

Run inside the web container:
    docker compose cp tests/test_scripts/watch_live_fusion.py web:/tmp/w.py
    docker compose exec -T web sh -c "cd /code && cp /tmp/w.py /code/w.py && python /code/w.py"
"""
import os
import threading
import time

import httpx
from sqlalchemy import select

from app.config import ADMIN_TOKEN
from app.db import SessionLocal
from app.models import ExtractionSession, ExtractionUnit

# The container's own port, not WEB_PORT. WEB_PORT is what the port is mapped
# to on the host; inside, uvicorn binds 8000 (confirmed from /proc/net/tcp).
BASE = f"http://127.0.0.1:{os.getenv('WEB_INTERNAL_PORT', '8000')}"
POLL_SECONDS = 3
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")


def pick_unit():
    """A finished unit of the largest real session -- the most pages to watch."""
    with SessionLocal() as db:
        session = db.scalars(
            select(ExtractionSession).order_by(ExtractionSession.created_at.desc())
        ).first()
        for candidate in db.scalars(
            select(ExtractionSession).order_by(ExtractionSession.created_at.desc())
        ).all():
            units = db.scalars(
                select(ExtractionUnit)
                .where(ExtractionUnit.session_id == candidate.id)
                .order_by(ExtractionUnit.unit_number)
            ).all()
            if units:
                session = candidate
                break
        units = db.scalars(
            select(ExtractionUnit)
            .where(ExtractionUnit.session_id == session.id)
            .order_by(ExtractionUnit.unit_number)
        ).all()
        best = max(units, key=lambda u: (u.page_to or 0) - (u.page_from or 0))
        return (session.id, session.document_id, best.id, best.unit_number,
                best.page_from, best.page_to, session.status)


def main():
    (session_id, document_id, unit_id, number,
     page_from, page_to, session_status) = pick_unit()
    session_id, document_id, unit_id = str(session_id), str(document_id), str(unit_id)
    print(f"session {session_id[:8]} status={session_status}")
    print(f"re-running unit {number}: pages {page_from}-{page_to} ({unit_id[:8]})")
    print()

    # Run the task's own body in a background thread rather than going through
    # Redis. `apply_async` from this container does publish, but the worker's
    # Celery app does not pick the message up, and debugging the queue is not
    # what this script is for. `process_unit.run(...)` is the same function the
    # worker calls, minus the transport -- and the transport is not what is
    # being tested.
    from app.tasks import process_unit

    thread = threading.Thread(
        target=process_unit.run,
        args=(document_id, session_id, unit_id),
        daemon=True,
    )
    thread.start()
    print(f"process_unit started in a thread (alive={thread.is_alive()})")
    print()
    print("  time   cards_live  cards_done  headline  saved   note")
    print("  " + "-" * 66)

    started = time.time()
    last = None
    seen_live = 0
    distinct = set()
    client = httpx.Client(base_url=BASE, auth=httpx.BasicAuth(ADMIN_USER, ADMIN_TOKEN),
                          timeout=30.0)

    while time.time() - started < 420:
        try:
            data = client.get(f"/sessions/{session_id}/status").json()
        except Exception as exc:
            print(f"  poll failed: {type(exc).__name__}")
            break

        p = data.get("progress") or {}
        live = p.get("cards_live")
        cards = p.get("cards_done") or 0
        headline = p.get("records_headline") or 0
        saved = p.get("records_settled") or 0
        running_unit = p.get("running_unit_number")

        elapsed = int(time.time() - started)
        if live:
            seen_live += 1
            distinct.add(cards)
        if (live, cards) != last:
            note = f"page {p['active_page']}" if p.get("active_page") else ""
            print(f"  {elapsed:>4}s  {str(live):<10} {cards:>10} {headline:>9} {saved:>6}   "
                  f"{note}{(' unit ' + str(running_unit)) if running_unit else ''}")
            last = (live, cards)

        # The session row stays 'completed' throughout, because only
        # process_unit is re-run and finalize() is not. So the loop watches the
        # thread and the counter rather than session status: it ends when the
        # work is done AND the counter has been live at least once, or when
        # the thread has died without ever going live (which is the failure
        # this script exists to catch).
        if not thread.is_alive() and elapsed > 5:
            time.sleep(2)
            if not live:
                print(f"\n  work finished at +{elapsed}s without the counter going live")
            else:
                print(f"\n  work finished at +{elapsed}s")
            break
        if elapsed > 30 and seen_live and not live:
            print(f"\n  card counter stopped at +{elapsed}s")
            break
        time.sleep(POLL_SECONDS)

    print()
    print(f"polls with a live card counter : {seen_live}")
    print(f"distinct card counts observed  : {len(distinct)}")
    print(f"cards range                    : {min(distinct) if distinct else '-'} .. "
          f"{max(distinct) if distinct else '-'}")
    if seen_live == 0:
        print("\nFAIL: the card counter never went live. The headline fell back to")
        print("      the database estimate, which is the original complaint.")
    elif len(distinct) < 3:
        print("\nFAIL: the counter went live but barely moved -- it is not per-card.")
    else:
        print("\nOK: the headline climbed card by card while work was happening.")
        print("    A jump-only counter would show one distinct value per batch.")

    # The counter must never outlive the work it describes.
    final = client.get(f"/sessions/{session_id}/status").json()
    fp = final.get("progress") or {}
    db_total = sum(
        int(u.get("records_extracted") or 0) for u in (final.get("units") or [])
    )
    print(f"\nfinal: state={fp.get('state')} settled={fp.get('records_settled')} "
          f"headline={fp.get('records_headline')} units_sum={db_total} "
          f"cards_live={fp.get('cards_live')}")
    assert fp.get("cards_live") is False, (
        "the card counter is still live with no unit running -- it is describing "
        "a request that has ended"
    )
    print("OK: the counter stops dead when the work stops.")


if __name__ == "__main__":
    main()