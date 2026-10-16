"""Compare a fresh run against the captured baseline, field by field, by EPIC.

Usage: python3 compare_to_baseline.py <label> [baseline_file]

Default baseline is $HOME/ocr-baseline/baseline_run1_p6-8.json.
Exits 0 only when every shared record matches on every shared field.
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import os

SERVICE = os.environ.get("OCR_URL", "http://127.0.0.1:8082/ocr/extract")
PDF = Path(os.environ.get("BASELINE_PDF", "sample-pdfs/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"))
OUT = Path(os.environ.get("OCR_BASELINE_DIR", str(Path.home() / "ocr-baseline")))


def post(start, end):
    boundary = "----cmp" + uuid.uuid4().hex
    pdf = PDF.read_bytes()
    parts = [f"--{boundary}\r\n",
             'Content-Disposition: form-data; name="pdf_file"; filename="roll.pdf"\r\n',
             "Content-Type: application/pdf\r\n\r\n", pdf.decode("latin-1")]
    for name, value in (("start_page", str(start)), ("end_page", str(end))):
        parts += [f"\r\n--{boundary}\r\n",
                  f'Content-Disposition: form-data; name="{name}"\r\n\r\n', value]
    parts.append(f"\r\n--{boundary}--\r\n")
    req = urllib.request.Request(
        SERVICE, data="".join(parts).encode("latin-1"),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=3600) as resp:
        return json.loads(resp.read().decode("utf-8"))


def epic_key(rec):
    for field in ("epic_no", "id_card_no", "voter_sr_no"):
        value = rec.get(field)
        if value:
            return f"{field}={value}"
    return f"sno={rec.get('sno')}"


# Compared on purpose. Timing/elapsed fields and instrument-only keys are
# expected to move; extracted voter data must not.
COMPARE = [
    "epic_no", "id_card_no", "voter_sr_no", "sno", "gender", "age",
    "house_no", "voter_first_name", "voter_middle_name", "voter_sur_name",
    "relation_name", "is_deleted", "state_code", "ac_code", "anubhag_code",
    "booth_code", "needs_review", "review_reasons",
]
for slot in ("husband", "father", "mother", "other"):
    for part in ("first", "middle", "last"):
        COMPARE.append(f"voter_{slot}_{part}_name")


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "phase1"
    base_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT / "baseline_run1_p6-8.json"
    base = json.loads(base_path.read_text())

    t0 = time.perf_counter()
    new = post(6, 8)
    wall = time.perf_counter() - t0
    (OUT / f"{label}_p6-8.json").write_text(
        json.dumps(new, indent=2, ensure_ascii=False))

    bc, nc = base.get("tesseract_calls", {}), new.get("tesseract_calls", {})
    print(f"=== {label} vs {base_path.name} ===")
    print(f"wall: {wall:.1f}s   (baseline run1 183.3s / run2 154.1s)")
    print(f"elapsed_seconds: {new.get('elapsed_seconds')}")
    print(f"records: {base.get('records_count')} -> {new.get('records_count')}")
    print(f"needs_review: {base.get('records_needing_review')} -> "
          f"{new.get('records_needing_review')}")
    print(f"\n--- launches ({sum(bc.values())} -> {sum(nc.values())}) ---")
    for key in sorted(set(bc) | set(nc)):
        b, n = bc.get(key, 0), nc.get(key, 0)
        delta = f"{n - b:+d}" if b or n else ""
        print(f"  {key:20} {b:6} -> {n:6}  {delta}")

    ib = {epic_key(r): r for r in base.get("records", [])}
    inw = {epic_key(r): r for r in new.get("records", [])}
    only_b, only_n = sorted(set(ib) - set(inw)), sorted(set(inw) - set(ib))
    diffs = []
    for key in sorted(set(ib) & set(inw)):
        for field in COMPARE:
            if ib[key].get(field) != inw[key].get(field):
                diffs.append((key, field, ib[key].get(field), inw[key].get(field)))

    print(f"\n--- field diff over {len(set(ib) & set(inw))} matched records ---")
    print(f"records only in baseline: {len(only_b)}")
    print(f"records only in new     : {len(only_n)}")
    print(f"differing fields        : {len(diffs)}")
    for key, field, vb, vn in diffs[:20]:
        print(f"  {key} {field}: {vb!r} -> {vn!r}")
    for key in only_b[:5]:
        print(f"  only baseline: {key}")
    for key in only_n[:5]:
        print(f"  only new: {key}")

    if not only_b and not only_n and not diffs:
        print("\nPASS - field-identical to baseline")
        return 0
    print("\nFAIL - output differs from baseline")
    return 1


if __name__ == "__main__":
    sys.exit(main())