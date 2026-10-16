"""Run the OCR service over the ground-truth pages and diff, field by field, by EPIC.

Matches per-page ground truth files under ground_truth/json_files/. EPIC identity
via epic_no / id_card_no / voter_sr_no, never row position.
"""
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

SERVICE = os.environ.get("OCR_URL", "http://127.0.0.1:8082/ocr/extract")
PDF = Path("sample-pdfs/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
GT = Path("ground_truth/json_files")


def post(start, end):
    boundary = "----gt" + uuid.uuid4().hex
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
    for f in ("epic_no", "id_card_no", "voter_sr_no"):
        v = rec.get(f)
        if v:
            return str(v)
    return f"sno={rec.get('sno')}"


# Fields that are pipeline-computed metadata, or semantics that differ
# (gt sno is per-page; pipeline sno is global). Excluded from scoring.
SKIP = {"field_sources", "page_number", "card_index", "pdf_name", "review_reasons",
        "needs_review", "anubhag_name", "confidence", "confidence_coverage", "sno"}


def main():
    pages = [int(x) for x in sys.argv[1].split(",")] if len(sys.argv) > 1 else [3, 4, 5, 6, 7, 8, 20, 21]
    # Group into contiguous runs to minimise POSTs
    runs = []
    for p in sorted(pages):
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])

    total_match, total_fields, total_matched_recs, total_recs = 0, 0, 0, 0
    for start, end in runs:
        t0 = time.perf_counter()
        new = post(start, end)
        wall = time.perf_counter() - t0
        gt_recs = []
        for p in range(start, end + 1):
            d = json.loads((GT / f"page{p}_results.json").read_text())
            gt_recs.extend(d["records"])
        ib = {epic_key(r): r for r in gt_recs}
        inw = {epic_key(r): r for r in new.get("records", [])}
        only_gt, only_new = sorted(set(ib) - set(inw)), sorted(set(inw) - set(ib))
        matched = set(ib) & set(inw)
        diffs, fields_per = [], 0
        for key in matched:
            for field in set(ib[key]) | set(inw[key]):
                if field in SKIP:
                    continue
                fields_per += 1
                if ib[key].get(field) != inw[key].get(field):
                    diffs.append((key, field, ib[key].get(field), inw[key].get(field)))
        total_match += fields_per - len(diffs)
        total_fields += fields_per
        total_matched_recs += len(matched)
        total_recs += len(gt_recs)
        acc = 100.0 * (fields_per - len(diffs)) / max(1, fields_per)
        print(f"pages {start}-{end}: wall {wall:.1f}s  records {len(gt_recs)} -> {len(new.get('records', []))}"
              f"  matched {len(matched)}  field acc {acc:.1f}%  diffs {len(diffs)}  only_gt {len(only_gt)}  only_new {len(only_new)}")
        for d in diffs[:10]:
            print(f"   {d[0]} {d[1]}: gt={d[2]!r} new={d[3]!r}")

    acc = 100.0 * total_match / max(1, total_fields)
    print(f"\nTOTAL: {total_recs} gt records, {total_matched_recs} matched, field accuracy {acc:.2f}% ({total_match}/{total_fields})")


if __name__ == "__main__":
    main()
