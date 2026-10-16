"""Save a measured card layout. Nothing in the pipeline reads it back.

An unknown roll is still read by the label reader. This module writes the
offsets it measured — name, relative, house, age, gender, each from the
voter ID — into an unconfirmed JSON file. The scanned reader and the Jaipur
reader never load that file.

The file is written only after a golden check: the booth roll, when it is
on disk, must still classify as ``scanned``. A failed check writes nothing
and leaves the current read alone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "roll_templates" / "unconfirmed"

# The live booth roll. Present in the worker via the sample-pdfs mount.
# Classifying it as anything else means the gate moved, so a new template
# must not be stored.
_BOOTH_ROLL = (
    "/data/pdfs/minio-BoothWise-Voter-After-SIR-2026-41eb391f2751/"
    "BoothWise Voter After SIR 2026/"
    "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-5-WI.pdf"
)

_GOLDEN: bool | None = None


def save_unconfirmed(result, route_kind: str) -> Path | None:
    """Write one unconfirmed template. Returns the path, or None when skipped."""
    if route_kind not in ("new-card-format", "scanned-labels"):
        return None
    if not result.anchors:
        return None
    if not _golden_ok():
        return None

    template = _compile(result, route_kind)
    _DIR.mkdir(parents=True, exist_ok=True)
    path = _DIR / f"{template['id']}.json"
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
    return path


def _compile(result, route_kind: str) -> dict:
    labels: dict[str, dict] = {}
    widths = []
    heights = []
    xs = []
    for card in result.anchors:
        widths.append(card.get("card_width"))
        heights.append(card.get("card_height"))
        xs.append(card.get("card_x"))
        for field, offset in card.items():
            if field in ("card_width", "card_height", "card_x"):
                continue
            if not isinstance(offset, dict):
                continue
            labels.setdefault(field, []).append(offset)

    compiled = {
        field: {"dx": _median([item["dx"] for item in offsets]),
                "dy": _median([item["dy"] for item in offsets])}
        for field, offsets in labels.items()
        if len(offsets) * 2 >= len(result.anchors)
    }
    width = _median([value for value in widths if value])
    height = _median([value for value in heights if value])
    columns = _columns([value for value in xs if value is not None])
    identity = f"c{columns}_w{width}_h{height}_{'_'.join(sorted(compiled))}"
    page_width, page_height = result.page_size
    return {
        "id": identity,
        "status": "unconfirmed",
        "route": route_kind,
        "page_width": page_width,
        "page_height": page_height,
        "columns": columns,
        "card_width": width,
        "card_height": height,
        "labels": compiled,
        "cards_measured": len(result.anchors),
        "note": (
            "Sample only. The scanned reader and the text reader do not "
            "load this file."
        ),
    }


def _golden_ok() -> bool:
    """True when the booth roll still belongs to the scanned reader."""
    global _GOLDEN
    if _GOLDEN is not None:
        return _GOLDEN
    if not os.path.exists(_BOOTH_ROLL):
        _GOLDEN = True
        return True
    from app.roll_gate import classify

    _GOLDEN = classify(_BOOTH_ROLL).kind == "scanned"
    return _GOLDEN


def _columns(xs: list[float]) -> int:
    clusters: list[float] = []
    for x in sorted(xs):
        if not clusters or abs(x - clusters[-1]) > 30:
            clusters.append(x)
    return max(1, len(clusters))


def _median(values: list[float]):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
