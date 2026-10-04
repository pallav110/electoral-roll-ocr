"""Does the headline climb card by card, using the real counter and no database?

Everything else here is faked at some level. This is not: a real PDF is
extracted by the real OCR service, the real /ocr/progress endpoint is polled
while it runs, and every snapshot goes through the real fuse_progress() with
the page range a real 3-5 unit would have.

No database is touched. That matters -- the alternative was resetting a
completed document back to `queued`, which would re-run the extraction and
rewrite live voter records to prove a display property.

The one link this cannot cover is Celery dispatch (document -> process_unit ->
OCR with the right page range). That link is already evidenced by the real
531-record session in the database, which was extracted through it.

    python tests/test_scripts/watch_card_counter.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from app.presentation import fuse_progress, live_progress  # noqa: E402

OCR = "http://127.0.0.1:8082"
PDF = os.path.join(HERE, "sample-pdfs",
                   "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
PAGE_FROM, PAGE_TO = 3, 5


class Unit:
    """The single running unit, as live_progress() sees one."""

    def __init__(self, status, page_from, page_to, records=0):
        self.unit_number = 1
        self.status = status
        self.page_from = page_from
        self.page_to = page_to
        self.records_extracted = records
        self.started_at = None
        self.completed_at = None


def post_extract(start, end):
    """POST the PDF as multipart without pulling in requests."""
    boundary = uuid.uuid4().hex
    with open(PDF, "rb") as fh:
        pdf = fh.read()

    def field(name, value):
        return (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n").encode()

    body = b"".join([
        field("start_page", start),
        field("end_page", end),
        field("whole_pdf", "false"),
        field("skip_non_voter_pages", "true"),
        (f"--{boundary}\r\n"
         'Content-Disposition: form-data; name="pdf_file"; filename="r.pdf"\r\n'
         "Content-Type: application/pdf\r\n\r\n").encode(),
        pdf,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{OCR}/ocr/extract", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def snapshot():
    try:
        with urllib.request.urlopen(f"{OCR}/ocr/progress", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def main():
    if not os.path.exists(PDF):
        print(f"no PDF at {PDF}")
        return 2

    print(f"pages {PAGE_FROM}-{PAGE_TO} against the live OCR service, "
          f"polling the real counter every 2s\n", flush=True)

    result = {}

    def work():
        result["response"] = post_extract(PAGE_FROM, PAGE_TO)

    thread = threading.Thread(target=work, daemon=True)
    started = time.time()
    thread.start()

    print("   time  live  cards  headline  page  note")
    print("  " + "-" * 58)

    seen = []
    last_row = None
    while thread.is_alive() and time.time() - started < 900:
        snap = snapshot()
        elapsed = int(time.time() - started)
        if snap:
            unit = Unit("processing", PAGE_FROM, PAGE_TO)
            progress = live_progress([unit], pages_total=PAGE_TO - PAGE_FROM + 1)
            fused = fuse_progress(progress, snap, PAGE_FROM, PAGE_TO)

            row = (fused["cards_live"], fused["cards_done"], fused["records_headline"])
            if row != last_row:
                note = ""
                if fused["cards_done"]:
                    note = (f"{fused['cards_records']} of {fused['cards_done']} cards "
                            f"produced a record")
                print(f"  {elapsed:>5}s  {str(fused['cards_live']):<5} "
                      f"{fused['cards_done']:>6} {fused['records_headline']:>9}  "
                      f"{str(fused['active_page'] or '-'):>4}  {note}")
                last_row = row
            if fused["cards_live"]:
                seen.append(fused["cards_done"])
        time.sleep(2)

    thread.join(timeout=60)
    elapsed = int(time.time() - started)

    response = result.get("response") or {}
    produced = len(response.get("records") or [])
    print(f"\n  extraction finished in {elapsed}s, {produced} records")

    distinct = sorted(set(seen))
    print(f"  polls with a live counter : {len(seen)}")
    print(f"  distinct card counts      : {len(distinct)}")
    if distinct:
        print(f"  card range                : {distinct[0]} .. {distinct[-1]}")
    print(f"  final counter cards_records: "
          f"{(snapshot() or {}).get('cards_records')}")

    print()
    if not seen:
        print("FAIL: the counter never went live.")
        return 1
    if len(distinct) < 5:
        print(f"FAIL: only {len(distinct)} distinct values -- that is a per-batch "
              f"counter, not a per-card one.")
        return 1
    if max(seen) > produced:
        print(f"FAIL: counter reached {max(seen)} cards but only {produced} records "
              f"were produced.")
        return 1

    final = snapshot() or {}
    assert final.get("cards_records") == produced, (
        f"final counter says {final.get('cards_records')} records, "
        f"response has {produced}")
    print(f"OK: {len(distinct)} distinct values from {distinct[0]} to "
          f"{distinct[-1]} -- the headline moves per card, not per batch.")
    print(f"OK: final counter ({produced}) matches the response exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())