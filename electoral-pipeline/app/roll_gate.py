"""Decide which reader, if any, may see a PDF.

Four answers, and the file is not sent anywhere until one of them is chosen:

- ``scanned``           image-only, and the voter IDs sit on the booth grid
                        the scanned reader was measured on. That reader.
- ``scanned-labels``    image-only, with voter IDs off that grid. The label
                        reader, after the page is OCR'd.
- ``text-ward``         the Jaipur municipal header; the text reader on 8083
- ``not-a-roll``        a text layer and no voter cards (a kundli). Refused.
- ``new-card-format``   a text layer with voter IDs the ward reader does not
                        own. The label reader.

A file that cannot be opened is ``unclassified``. The caller keeps its
previous default rather than inventing a refusal from a read error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Same two ID shapes the text reader anchors a card on. OCR of a scan sometimes
# drops or adds a digit, so six to eight digits still count as an ID. A bare
# number is a serial or a house number and must not become a card.
_ID_PATTERN = re.compile(r"^(?:[A-Z]{3}\d{6,8}|[A-Z]{2}/\d{2}/\d{3}/\d{6})$")
_MIN_IDS = 4
_SAMPLE_PAGES = 8

# The booth roll's 3-by-10 card grid, copied from ocr_pdf_api. A scanned page
# whose voter IDs fall in these cells is that template and keeps going to the
# scanned reader. Anything else with real voter IDs is a new layout.
_ROW_BOUNDS = (
    (0.0325, 0.1209), (0.1266, 0.2155), (0.2213, 0.3099), (0.3155, 0.4042),
    (0.4094, 0.4982), (0.5035, 0.5921), (0.5975, 0.6863), (0.6917, 0.7806),
    (0.786, 0.8748), (0.8802, 0.969),
)
_LEFT_MARGIN = 0.011
_RIGHT_MARGIN = 0.019
_COLUMN_GAP = 0.0055
_CELL_SLACK = 0.02


@dataclass
class GateResult:
    kind: str
    boxes: list[dict] = field(default_factory=list)


# One probe per file. Every unit of a roll asks again, and a probe is an OCR.
_SCAN_CACHE: dict[str, GateResult] = {}


def classify(path: str) -> GateResult:
    """Classify one PDF. ``boxes`` is set only for ``new-card-format``."""
    try:
        import fitz
    except ImportError:
        return GateResult("unclassified")
    try:
        document = fitz.open(path)
    except Exception:
        return GateResult("unclassified")

    with document:
        if document.page_count == 0:
            return GateResult("unclassified")
        any_text = False
        ward = False
        best: list[dict] = []
        for index in _sample_indexes(document.page_count):
            page = document[index]
            text = page.get_text("text") or ""
            if text.strip():
                any_text = True
            if not ward and _page_has_ward_header(text):
                ward = True
            boxes = _boxes_for_page(page, index + 1)
            if len(boxes) > len(best):
                best = boxes
        if not any_text:
            return _classify_image_roll(document, path)
        if ward:
            return GateResult("text-ward")
        if len(best) >= _MIN_IDS:
            return GateResult("new-card-format", best)
        return GateResult("not-a-roll")


def format_box_list(boxes: list[dict]) -> str:
    """One line a document error can carry. Long pages keep the first 40."""
    shown = boxes[:40]
    parts = [
        f"p{box['page']} {box['id']} "
        f"({box['x']},{box['y']} {box['width']}x{box['height']})"
        for box in shown
    ]
    text = "; ".join(parts)
    if len(boxes) > len(shown):
        text += f"; +{len(boxes) - len(shown)} more"
    return text


def _sample_indexes(page_count: int, limit: int = _SAMPLE_PAGES) -> list[int]:
    if page_count <= limit:
        return list(range(page_count))
    step = max(1, page_count // limit)
    indexes = list(range(0, page_count, step))[:limit]
    # The last page of a roll is often the supplement, where the cards are.
    if (page_count - 1) not in indexes:
        indexes[-1] = page_count - 1
    return indexes


def _page_has_ward_header(text: str) -> bool:
    """The Jaipur ward label, matched by the router's own function.

    Imported lazily so this module and extractor.py can call each other
    without a cycle while either file is still loading.
    """
    from app.extractor import _mentions_ward_header

    return any(_mentions_ward_header(line) for line in text.splitlines())


def _id_token(text: str) -> str:
    return (text or "").strip().strip(".,;:|").upper()


def _classify_image_roll(document, path: str) -> GateResult:
    """Image-only file: the booth grid stays on its reader, a new grid does not.

    No voter IDs, or no OCR engine, keeps today's answer and sends the file
    to the scanned reader. That reader already rejects a page with no grid,
    which is where an image-only kundli stops.
    """
    cached = _SCAN_CACHE.get(path)
    if cached is not None:
        return cached

    from app.ocr_adapter import ocr_page_words

    result = GateResult("scanned")
    for index in _probe_indexes(document.page_count):
        page = document[index]
        words = ocr_page_words(page)
        found = _ids_on_booth_grid(words, page)
        if found == "known":
            result = GateResult("scanned")
            break
        if found == "other":
            result = GateResult(
                "scanned-labels",
                _boxes_for_page(page, index + 1, words=words),
            )
            break
    _SCAN_CACHE[path] = result
    return result


def _probe_indexes(page_count: int) -> list[int]:
    """A voter page, then one further in. Page 1 of these rolls is the cover."""
    if page_count <= 1:
        return [0]
    first = min(2, page_count - 1)
    second = min(max(page_count // 2, first + 1), page_count - 1)
    indexes = [first]
    if second != first:
        indexes.append(second)
    return indexes


def _column_bounds() -> list[tuple[float, float]]:
    span = 1.0 - _LEFT_MARGIN - _RIGHT_MARGIN
    width = (span - _COLUMN_GAP * 2) / 3
    x = _LEFT_MARGIN
    columns = []
    for _ in range(3):
        columns.append((x - _CELL_SLACK, x + width + _CELL_SLACK))
        x += width + _COLUMN_GAP
    return columns


def _ids_on_booth_grid(words: list, page) -> str:
    """``known`` if the IDs sit in the booth cells, ``other`` if they do not.

    ``few`` means there is not enough to call it a new layout, so the scanned
    reader keeps the file.
    """
    ids = [word for word in words if _ID_PATTERN.match(_id_token(word[4]))]
    if len(ids) < _MIN_IDS:
        return "few"
    width = page.rect.width or 1
    height = page.rect.height or 1
    columns = _column_bounds()
    hits = 0
    used = set()
    for word in ids:
        rx = ((word[0] + word[2]) / 2) / width
        ry = ((word[1] + word[3]) / 2) / height
        column = next((i for i, (a, b) in enumerate(columns) if a <= rx <= b), None)
        row = next(
            (i for i, (a, b) in enumerate(_ROW_BOUNDS)
             if a - _CELL_SLACK <= ry <= b + _CELL_SLACK),
            None,
        )
        if column is not None and row is not None:
            hits += 1
            used.add(column)
    ratio = hits / len(ids)
    if hits >= 6 and ratio >= 0.6 and len(used) >= 2:
        return "known"
    return "other"


def _boxes_for_page(page, page_number: int, min_ids: int = _MIN_IDS, words=None) -> list[dict]:
    """One box per voter ID, sized from the gaps between the IDs themselves.

    The box is anchored on the ID, which is the one token both known rolls
    print on every card. Width and height come from the pitch of those IDs
    on this page, so an English card is not forced into the Jaipur card size.

    ``min_ids`` is 4 when the gate is deciding what a file is. The adapter
    passes 1 so a short last row is still read.
    """
    if words is None:
        words = page.get_text("words") or []
    ids = [word for word in words if _ID_PATTERN.match(_id_token(word[4]))]
    if len(ids) < min_ids:
        return []

    ids.sort(key=lambda word: (word[1], word[0]))
    rows: list[list] = []
    for word in ids:
        if not rows or abs(word[1] - rows[-1][0][1]) > 18:
            rows.append([word])
        else:
            rows[-1].append(word)

    gaps_x = []
    for row in rows:
        row.sort(key=lambda word: word[0])
        for left, right in zip(row, row[1:]):
            gap = right[0] - left[0]
            if gap > 40:
                gaps_x.append(gap)
    row_tops = [min(word[1] for word in row) for row in rows]
    gaps_y = []
    for above, below in zip(row_tops, row_tops[1:]):
        gap = below - above
        if gap > 24:
            gaps_y.append(gap)

    pitch_x = _median(gaps_x)
    pitch_y = _median(gaps_y)
    boxes = []
    for word in ids:
        # One column of cards has no horizontal neighbour, so the box runs
        # to the page edge. A measured pitch is used whenever there is one,
        # so two columns do not spill into each other.
        if pitch_x:
            width = round(pitch_x * 0.92, 1)
        else:
            width = round(max(160.0, page.rect.width - word[0] - 12.0), 1)
        if pitch_y:
            height = round(pitch_y * 0.90, 1)
        else:
            height = 140.0
        # The serial is printed just left of the ID, so the box starts a
        # short step before the token and stays inside the measured pitch.
        left_pad = min(40.0, width * 0.25)
        boxes.append({
            "page": page_number,
            "x": round(max(0.0, word[0] - left_pad), 1),
            "y": round(max(0.0, word[1] - 6.0), 1),
            "width": width,
            "height": height,
            "id": _id_token(word[4]),
        })
    return boxes


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
