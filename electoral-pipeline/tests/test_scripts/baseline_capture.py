"""Capture a baseline of current extraction output, plus Tesseract call counts.

Follows the shape of golden_diff_6_8.py, including its most important property:
run the SAME build twice first and measure run-to-run nondeterminism. If two
identical runs disagree, a single later diff proves nothing.

Matching is by EPIC, never by row position -- ground_truth/README.md states the
reference set carries one extra leading row (the tail card of page 5), so row N
of the reference is card N+1 of page 6. Position matching reports a false diff.

Usage: python3 baseline_capture.py <label> <start> <end>
"""
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

SERVICE = "http://127.0.0.1:8082/ocr/extract"
PDF = Path("sample-pdfs/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
OUT = Path(os.environ.get("OCR_BASELINE_DIR", str(Path.home() / "ocr-baseline")))

BOUNDARY = "----baselinecapture"


def post(start, end):
    """POST the page range to the OCR service and return parsed JSON."""
    boundary = BOUNDARY + uuid.uuid4().hex
    pdf = PDF.read_bytes()
    parts = [
        f"--{boundary}\r\n",
        'Content-Disposition: form-data; name="pdf_file"; filename="roll.pdf"\r\n',
        "Content-Type: application/pdf\r\n\r\n",
    ]
    parts.append(pdf.decode("latin-1"))
    for name, value in (("start_page", str(start)), ("end_page", str(end))):
        parts.append(f"\r\n--{boundary}\r\n")
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        parts.append(value)
    parts.append(f"\r\n--{boundary}--\r\n")
    body = "".join(parts).encode("latin-1")

    req = urllib.request.Request(
        SERVICE, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def epic_key(rec):
    """Stable identity for a record. EPIC cannot collide; serials can."""
    for field in ("epic_no", "id_card_no", "voter_sr_no"):
        value = rec.get(field)
        if value:
            return f"{field}={value}"
    return f"sno={rec.get('sno')}"


def summarise(doc):
    calls = doc.get("tesseract_calls", {})
    return {
        "records_count": doc.get("records_count"),
        "pages_processed": doc.get("pages_processed"),
        "pages_skipped": doc.get("pages_skipped"),
        "needs_review": doc.get("records_needing_review"),
        "elapsed_seconds": doc.get("elapsed_seconds"),
        "tesseract_calls_total": sum(calls.values()),
        "tesseract_calls_by_kind": dict(sorted(calls.items())),
        "epics": [epic_key(r) for r in doc.get("records", [])],
    }


def diff_fields(a, b):
    """Field-level differences between two docs, matched by EPIC."""
    ia = {epic_key(r): r for r in a.get("records", [])}
    ib = {epic_key(r): r for r in b.get("records", [])}
    only_a, only_b = sorted(set(ia) - set(ib)), sorted(set(ib) - set(ia))
    fields = []
    for key in sorted(set(ia) & set(ib)):
        for field in sorted(set(ia[key]) | set(ib[key])):
            va, vb = ia[key].get(field), ib[key].get(field)
            if va != vb:
                fields.append((key, field, va, vb))
    return only_a, only_b, fields


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    end = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    OUT.mkdir(parents=True, exist_ok=True)
    docs = []
    for run in (1, 2):
        t0 = time.perf_counter()
        doc = post(start, end)
        wall = time.perf_counter() - t0
        path = OUT / f"{label}_run{run}_p{start}-{end}.json"
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
        s = summarise(doc)
        s["wall_seconds"] = round(wall, 2)
        print(f"--- run {run}: {wall:.1f}s wall ---")
        for k, v in s.items():
            if k != "epics":
                print(f"  {k}: {v}")
        print(f"  saved: {path}")
        docs.append(doc)

    print("\n=== run-to-run nondeterminism (same build, both runs) ===")
    only_a, only_b, fields = diff_fields(docs[0], docs[1])
    print(f"  records only in run1: {len(only_a)}")
    print(f"  records only in run2: {len(only_b)}")
    print(f"  differing fields:     {len(fields)}")
    for row in fields[:10]:
        print(f"    {row}")
    for row in only_a[:5]:
        print(f"    only run1: {row}")
    for row in only_b[:5]:
        print(f"    only run2: {row}")
    if not only_a and not only_b and not fields:
        print("  DETERMINISTIC - a later diff is meaningful")
    else:
        print("  NONDETERMINISTIC - a later diff proves nothing")


if __name__ == "__main__":
    main()