"""Measure every roll in a directory and report what is broken about it.

There is no ground truth here, and the design is deliberate: every check is a
statement about INTERNAL CONSISTENCY, so a roll can be flagged without knowing a
single correct value.  That is what makes the sweep usable on 161 rolls nobody
has hand-checked.

    card yield      records emitted / grid positions
    header fields   any of state/ac/booth/anubhag empty
    field population  names, relations, EPIC per card
    timing          seconds per page, against the 7-9s target

Two tiers, because full extraction over 161 rolls is ~13 hours:

    fast   render page 3 plus two spread pages, read every header field, count
           EPIC-shaped reads per card position.  No per-card Tesseract.  Catches
           geometry and header breakage -- the whole Lansdown class -- in about
           a minute per roll.
    full   complete extraction, for any roll fast flags.

Card yield is measured PER ROLL and never per page.  A tail page legitimately
emits 14 records out of 30 positions and the next emits 7; a per-page rule would
flag every one of them and bury the real signal.

Run inside the OCR container, where the module is the live bind-mounted copy:

    docker cp tools/corpus_sweep.py ocr:/app/
    docker exec ocr sh -c 'cd /app && python corpus_sweep.py --dir /data/pdfs --tier fast'
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

import cv2  # noqa: E402
import fitz  # noqa: E402
import numpy as np  # noqa: E402

import ocr_pdf_api as A  # noqa: E402

# Per-roll, not per-page: tail pages legitimately emit fewer records than grid
# positions, and the earlier S28-40 roll emits 531 of 660 (0.80) while being
# visibly short of its own grid.
YIELD_WARN = 0.90
YIELD_FAIL = 0.75
TARGET_PAGE_SECONDS = (7.0, 9.0)


def _safe(fn, default=None):
    """Never let one bad roll stop the sweep."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the point is to survive anything
        return default if default is not None else f"error: {type(exc).__name__}: {exc}"


def measure_geometry(page: fitz.Page) -> dict:
    """Positions the grid offers and how much ink sits in them."""
    rects = A._voter_card_rects(page)
    return {"positions": len(rects)}


def measure_lattice(page: fitz.Page) -> dict:
    """Compare the hardcoded row pitch against the pitch inferred from the ink.

    The inferred pitch comes from autocorrelation of the row-ink profile, so it
    knows nothing about the constants.  Agreement is evidence the geometry
    constants still describe this template; disagreement is the signal that the
    card grid itself is fitted to another layout.

    The peak is then tested against BOTH harmonics of itself.  A row profile is
    periodic at one row pitch, so its autocorrelation peaks equally at 1x and 2x
    that pitch -- the measured 10-row grid peaks at 9.41% and at 18.86%, and the
    raw peak is the 2x one.  Reporting that raw peak against the constant's 1x
    flags every roll identically, which is what a first version of this did.  So
    the inferred value is whichever harmonic is closer to the constant, and the
    reported drift is only meaningful once both harmonics have been considered.

    Detection is SHADOW ONLY here -- computed and reported, never used to crop.
    A wrong lattice silently drops voters rather than returning a blank field,
    so it must not be allowed to drive extraction without its own review.
    """
    H = page.rect.height
    if H <= 0:
        return {}
    try:
        pix = page.get_pixmap(dpi=200, alpha=False)
        img = np.frombuffer(pix.samples, np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3]
    except Exception:
        return {}
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    otsu, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = gray <= int(min(otsu, 200))
    profile = dark.sum(axis=1).astype(np.float64)
    ph = profile.shape[0]
    if ph < 40:
        return {}
    kernel = np.ones(ph // 10) / max(1, ph // 10)
    centred = profile - np.convolve(profile, kernel, mode="same")

    scores = []
    for lag in range(ph // 14, ph // 4):
        a, b = centred[:-lag], centred[lag:]
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
        scores.append((float(a @ b) / denom, lag))
    if not scores:
        return {}
    scores.sort(reverse=True)
    best, lag = scores[0]
    peak = lag * 72.0 / 200.0 / H

    # What the constants say, for comparison.
    tops = sorted({round(r.y0 / H, 5) for r in A._voter_card_rects(page)})
    constant = (tops[1] - tops[0]) if len(tops) > 1 else None
    if not constant:
        return {"inferred_peak": round(peak, 4), "autocorr": round(best, 3)}

    # A periodic profile is equally periodic at every sub-multiple, so the peak
    # is compared against both its half and its whole.
    candidates = {"1x": peak, "2x": peak / 2.0, "3x": peak / 3.0}
    which = min(candidates, key=lambda k: abs(candidates[k] - constant))
    inferred = candidates[which]
    return {
        "inferred_peak": round(peak, 4),
        "inferred_pitch": round(inferred, 4),
        "harmonic": which,
        "constant_pitch": round(constant, 4),
        "drift": round(abs(inferred - constant), 4),
        "autocorr": round(best, 3),
    }


def measure_header(page: fitz.Page) -> dict:
    md = _safe(lambda: A._extract_roll_header_metadata(page), {}) or {}
    fields = ("state_code", "ac_code", "booth_code", "anubhag_code", "anubhag_name")
    present = {f: (md.get(f) or "") for f in fields}
    return {
        **{f: v for f, v in present.items()},
        "empty": [f for f, v in present.items() if not v],
        "review": md.get("anubhag_review_reason", ""),
    }


def measure_epics(page: fitz.Page) -> dict:
    """Count positions where a real EPIC is read, using the pipeline's own reader.

    An earlier version OCR'd the whole card and pattern-matched a token, which
    reported a card yield of 0.30 on a roll the pipeline extracts 30/30 records
    from: reading the entire card returns the serial, the house number and the
    age among everything else, and the first EPIC-shaped token in that soup is
    not the EPIC.

    `_extract_tesseract_epic` already does this correctly -- it crops the EPIC
    region, reads it, and applies `EPIC_SHAPE_RE`.  Reusing it means the sweep
    measures what the pipeline measures, rather than a second guess at it that
    can drift away from the real behaviour.
    """
    scale = 200 / 72.0
    pix = page.get_pixmap(dpi=200, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    found = []
    for r in A._voter_card_rects(page):
        crop = img[int(r.y0 * scale):int(r.y1 * scale),
                   int(r.x0 * scale):int(r.x1 * scale)]
        if crop.size == 0:
            continue
        ok, buf = cv2.imencode(".png", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
        if not ok:
            continue
        epic = _safe(lambda b=buf.tobytes(): A._extract_tesseract_epic(b), "")
        if epic:
            found.append(epic)
    return {"epic_hits": len(found), "sample": found[:3]}


def fast_sweep(path: Path, sample_pages: int = 3) -> dict:
    started = time.perf_counter()
    out = {"roll": path.name, "bytes": path.stat().st_size}
    doc = None
    try:
        doc = fitz.open(path)
        out["pages"] = doc.page_count
        if not doc.page_count:
            out["status"] = "no pages"
            return out

        # Page 3 carries the header; two spread pages sample the card grid.
        picks = sorted({2, doc.page_count // 2, max(2, doc.page_count - 2)})
        picks = picks[:sample_pages]

        pages = []
        for index in picks:
            if index >= doc.page_count:
                continue
            page = doc[index]
            pages.append({
                "page": index + 1,
                "geometry": _safe(lambda p=page: measure_geometry(p), {}),
                "lattice": _safe(lambda p=page: measure_lattice(p), {}),
                "epics": _safe(lambda p=page: measure_epics(p), {}),
            })

        header = _safe(lambda: measure_header(doc[2] if doc.page_count > 2 else doc[0]),
                       {})
        out["header"] = header
        out["pages_sampled"] = pages

        positions = max((p["geometry"].get("positions", 0) for p in pages), default=0)
        hits = max((p["epics"].get("epic_hits", 0) for p in pages), default=0)
        out["positions"] = positions
        out["epic_hits"] = hits
        out["card_yield"] = round(hits / positions, 3) if positions else None
        out["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        out["status"] = f"error: {type(exc).__name__}: {exc}"
    finally:
        if doc is not None:
            doc.close()
    out["seconds"] = round(time.perf_counter() - started, 1)
    return out


def judge(row: dict) -> list[str]:
    """The flags. A roll can carry several; each names its own cause."""
    flags = []
    y = row.get("card_yield")
    if y is not None:
        if y < YIELD_FAIL:
            flags.append(f"card_yield_critical:{y}")
        elif y < YIELD_WARN:
            flags.append(f"card_yield_low:{y}")
    for f in (row.get("header") or {}).get("empty", []):
        flags.append(f"header_empty:{f}")
    lat = next((p["lattice"] for p in row.get("pages_sampled", [])
                if p.get("lattice")), {})
    drift = lat.get("drift")
    if drift is not None and drift > 0.02:
        flags.append(f"lattice_drift:{drift}")
    return flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="/data/sweep")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    files = sorted(Path(args.dir).glob("*.pdf"))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"no PDFs under {args.dir}")
        return 1

    rows = []
    for i, path in enumerate(files, 1):
        row = fast_sweep(path)
        row["flags"] = judge(row)
        rows.append(row)
        mark = "!" if row["flags"] else " "
        print(f"[{i:>3}/{len(files)}] {mark} {row['seconds']:>6.1f}s "
              f"{row['roll'][:58]}"
              + (f"   {' '.join(row['flags'])}" if row["flags"] else ""),
              flush=True)

    # Corpus medians, for the checks that need one.
    yields = [r["card_yield"] for r in rows if r.get("card_yield") is not None]
    secs = [r["seconds"] for r in rows if r.get("status") == "ok"]
    summary = {
        "rolls": len(rows),
        "flagged": sum(1 for r in rows if r["flags"]),
        "median_yield": statistics.median(yields) if yields else None,
        "median_seconds": round(statistics.median(secs), 1) if secs else None,
        "target_page_seconds": TARGET_PAGE_SECONDS,
    }
    print("\n" + "=" * 74)
    print("SWEEP SUMMARY")
    print("=" * 74)
    for k, v in summary.items():
        print(f"  {k:24} {v}")
    if summary["flagged"]:
        print("\n  FLAGGED ROLLS:")
        for r in rows:
            if r["flags"]:
                print(f"    {r['roll'][:60]}")
                print(f"      {' '.join(r['flags'])}")

    out_path = Path(args.out + ".json")
    out_path.write_text(json.dumps({"summary": summary, "rows": rows},
                                   ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())