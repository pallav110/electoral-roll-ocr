"""Does the progress patch change a single extracted record?

This is the only question that matters about editing ocr_pdf_api.py. The
counter is meant to be read-only, and "meant to be" is not evidence.

So: run pages 6-8 against the live service twice and compare the two
responses record by record, field for field. The second run also checks the
frozen ground truth.

Runs from the host, because that is where the PDF and the ground truth live;
neither container mounts either.

    python tests/test_scripts/golden_diff_6_8.py

Exit 0 means identical. Otherwise it prints the first differing field.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

SERVICE = "http://127.0.0.1:8082/ocr/extract"
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PDF = os.path.join(HERE, "sample-pdfs",
                   "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
TRUTH = os.path.join(HERE, "ground_truth", "whole_pdf_6-8_results.json")

# Only fields that exist in BOTH the live response and the frozen ground truth.
#
# The ground truth was captured before the bilingual rename, so it carries
# voter_first_name / id_card_no where the service now emits voter_name /
# epic_no. Comparing the two directly reports every record as different --
# a schema mismatch, not an extraction change. Intersecting the key sets is
# what makes the comparison mean anything.
SHARED_FIELDS = [
    "sno", "voter_sr_no", "voter_sur_name",
    "voter_first_name", "voter_middle_name",
    "voter_father_first_name", "voter_father_middle_name", "voter_father_last_name",
    "gender", "age", "house_no", "id_card_no",
    "is_deleted", "needs_review", "review_reasons",
    "state_code", "ac_code", "booth_code", "anubhag_code",
]


def run(start, end):
    """POST the PDF as multipart, without pulling in requests."""
    boundary = uuid.uuid4().hex
    with open(PDF, "rb") as fh:
        pdf = fh.read()

    def field(name, value):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body = b"".join([
        field("start_page", start),
        field("end_page", end),
        field("whole_pdf", "false"),
        field("skip_non_voter_pages", "true"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="pdf_file"; '
            f'filename="{os.path.basename(PDF)}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode(),
        pdf,
        f"\r\n--{boundary}--\r\n".encode(),
    ])

    req = urllib.request.Request(
        SERVICE, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slim(response, fields):
    return [{f: r[f] for f in fields if f in r}
            for r in (response.get("records") or [])]


def first_difference(a, b):
    if len(a) != len(b):
        return f"record count differs: {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        for key in sorted(set(x) | set(y)):
            if x.get(key) != y.get(key):
                return f"record {i} field {key!r}: {x.get(key)!r} != {y.get(key)!r}"
    return None


def progress_now():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8082/ocr/progress", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def main():
    if not os.path.exists(PDF):
        print(f"no PDF at {PDF}")
        return 2

    start, end = 6, 8
    print(f"pages {start}-{end}, two runs, roughly 8-10 minutes total\n", flush=True)

    print("  run 1 ...", flush=True)
    t0 = time.time()
    baseline = run(start, end)
    b_slim = slim(baseline, SHARED_FIELDS)
    print(f"    {len(b_slim)} records in {time.time() - t0:.0f}s", flush=True)

    snap = progress_now()
    if snap:
        produced = len(baseline.get("records") or [])
        print(f"    counter after run: cards_done={snap['cards_done']} "
              f"cards_records={snap['cards_records']} "
              f"pages_done={snap['pages_done']} done={snap['done']}")
        print(f"    response records={produced}  "
              f"(counter cards_records should match)")
        assert snap["cards_records"] == produced, (
            f"counter says {snap['cards_records']} records, "
            f"response has {produced}"
        )
        assert snap["cards_done"] >= snap["cards_records"], (
            "grid positions must be >= records: a blank lattice slot is "
            "work done with no record produced"
        )
        print(f"    OK: counter agrees with the response")

    print("\n  run 2 (same code, to measure run-to-run noise) ...", flush=True)
    t0 = time.time()
    patched = run(start, end)
    p_slim = slim(patched, SHARED_FIELDS)
    print(f"    {len(p_slim)} records in {time.time() - t0:.0f}s", flush=True)

    print()
    print(f"  run 1: {len(b_slim)} records")
    print(f"  run 2: {len(p_slim)} records")

    diff = first_difference(b_slim, p_slim)
    print()
    if diff:
        print(f"  run1 vs run2: DIFFERS -- {diff}")
        print("  This is the patched code disagreeing with itself, which means")
        print("  the run is not deterministic and a single diff proves nothing.")
        print("  Compare against ground truth instead.")
    else:
        print("  run1 vs run2: identical")

    if not os.path.exists(TRUTH):
        print(f"\n  no ground truth at {TRUTH}")
        return 1 if diff else 0

    with open(TRUTH, encoding="utf-8") as fh:
        truth = json.load(fh)
    t_records = truth.get("records") if isinstance(truth, dict) else truth
    t_slim = [{f: r[f] for f in SHARED_FIELDS if f in r} for r in t_records]

    print(f"\n  ground truth: {len(t_slim)} records")
    print(f"  fields compared ({len(SHARED_FIELDS)}): {', '.join(SHARED_FIELDS)}")
    tdiff = first_difference(t_slim, p_slim)
    if tdiff:
        print(f"  patched vs ground truth: DIFFERS -- {tdiff}")
        return 1
    print("  patched vs ground truth: IDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())