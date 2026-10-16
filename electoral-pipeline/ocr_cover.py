"""Cover-page reader for electoral rolls.

Page 1 of an ECI roll is a form, not a card grid. It names the state, the
assembly constituency, the part number, the revision, the polling station and
its address, and -- in section 4 -- a six-cell table:

    start serial | end serial | male | female | third gender | total

Those printed counts are the authority the card OCR is checked against: if the
cards read 530 males and the cover says 539, nine cards went wrong somewhere.

Two sources, one shape. A born-digital roll has a text layer and is parsed
from it directly. A scanned roll is rendered and read with Tesseract: the
labelled fields from a full-page pass, the counts cell by cell after finding
the table's grid lines, because a whole-page pass drops single digits (the
"1" of a start serial, the "0" of third gender) that a tight cell crop keeps.

Nothing here is guessed to fill a gap. A field that cannot be read is left
empty, and the raw page text is kept so the operator can see what was there.
"""
from __future__ import annotations

import re
import time
from typing import Any

import cv2
import fitz
import numpy as np
import pytesseract

COVER_RENDER_DPI = 300
TEXT_LAYER_MIN_CHARS = 200

# Column order of section 4 on the ECI form. Fixed by the form, not inferred.
COUNT_COLUMNS = ("start_serial", "end_serial", "male", "female", "third_gender", "total")

# Label spellings as Tesseract's hin model returns them. The matras drift
# (पुनरीक्षण/पुनरिक्षण, वार्ड/बार्ड), so labels are matched on a stable stem.
_LABELS = {
    "revision_year": r"पुनर[ीि]क्षण\s*का\s*वर्ष",
    "qualifying_date": r"अर्हता\s*तिथि|अहर्ता\s*तिथि|अर्हता\s*दिनांक",
    "revision_type": r"पुनर[ीि]क्षण\s*का\s*प्रकार",
    "publication_date": r"प्रकाशन\s*की\s*तिथि",
    "village": r"(?:मुख्य\s*)?कस्बा\s*अथवा\s*ग्राम",
    # "वार्ड संख्या : 28" is the municipal form. A bare "वार्ड" is the ECI
    # locality field. "वार्ड का परिसीमन" is a section heading on the
    # municipal cover and must not be read as the ward's value.
    "ward": r"वार्ड\s*संख्या|[वब]ार्ड(?!\s*का)",
    "post_office": r"डाकघर",
    "police_station": r"थाना",
    "panchayat": r"पंचायत",
    "block": r"ब्ल[ॉा]क",
    "tehsil": r"तहसील",
    "district": r"जिला",
    "pin_code": r"पिन\s*कोड",
}
_LOCALITY_KEYS = ("village", "ward", "post_office", "police_station", "panchayat",
                  "block", "tehsil", "district", "pin_code")
# Anything OCR tends to put where the form prints a colon. Horizontal space
# only -- a newline must never let an empty value swallow the next line.
_SEP = r"[ \t]*[:：+४*३$;।|_\-]*[ \t]*"
_D = r"[0-9]"
_DATE = rf"({_D}{{1,2}}[ \t]*[-/.][ \t]*{_D}{{1,2}}[ \t]*[-/.][ \t]*{_D}{{4}})"


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"[|_]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" :：+*;।$-")
    # A lone leftover character before the value is a colon the OCR turned
    # into a digit or symbol ("2 गाजियाबाद", "$ अपरकोट").
    value = re.sub(r"^[^\u0900-\u097F\w]\s+", "", value)
    value = re.sub(r"^[0-9]\s+(?=[\u0900-\u097F])", "", value)
    return value.strip()


def _digits(value: str | None) -> str:
    return re.sub(r"[^0-9]", "", value or "")


def _int_or_none(value: Any) -> int | None:
    text = _digits(str(value)) if value is not None else ""
    return int(text) if text else None


def _first(pattern: str, text: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else ""


_STOP_LABELS = list(_LABELS.values()) + [
    r"मतदान\s*स्थल", r"इस\s*भाग", r"निर्वाचक\s*नामावली\s*की\s*पहचान",
]


def _after_label(label: str, text: str) -> str:
    """Value printed after `label` on the same line, up to the next label.

    A label at the start of a line wins over the same word inside a section
    name earlier on the page ("बी ब्लाक से 83" is not the block field).
    """
    match = re.search(rf"(?m)^[ \t]*(?:{label}){_SEP}([^\n]*)", text)
    if not match:
        match = re.search(rf"(?:{label}){_SEP}([^\n]*)", text)
    if not match:
        return ""
    value = match.group(1)
    cut = len(value)
    for stop in _STOP_LABELS:
        hit = re.search(stop, value)
        if hit and 0 < hit.start() < cut:
            cut = hit.start()
    return _clean(value[:cut])


def _split_code_name(value: str) -> tuple[str, str, str]:
    """'53 - लोनी (सामान्य)' -> ('53', 'लोनी', 'सामान्य')."""
    value = _clean(value)
    match = re.match(rf"({_D}+)\s*[-–—]?\s*(.*)", value)
    if not match:
        return "", value, ""
    code, rest = match.group(1), match.group(2)
    reservation = _first(r"\(([^)]*)\)", rest)
    name = _clean(re.sub(r"\([^)]*\)", "", rest))
    name = re.split(r"भाग\s*संख्या", name)[0].strip()
    return code, name, _clean(reservation)


def parse_cover_text(raw: str) -> dict[str, Any]:
    """Labelled fields from the cover page text, whichever engine produced it."""
    text = raw.replace("\u200d", "").replace("\u200c", "")
    lines = [_clean(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    joined = "\n".join(lines)
    out: dict[str, Any] = {}

    title = _first(rf"निर्वाचक\s*नामावली\s*[-–]?\s*({_D}{{4}}[^\n]*)", joined)
    out["roll_year"] = _int_or_none(_first(rf"({_D}{{4}})", title))
    # "S24" arrives as S24, $24, 524 or ५24 depending on the model's mood.
    state_match = re.search(rf"{_D}{{4}}\s*[S$5५]?\s*({_D}{{2}})\s*(.*)", title)
    out["state_code"] = state_match.group(1) if state_match else ""
    out["state_name"] = _clean(state_match.group(2)) if state_match else ""
    if not out["state_name"]:
        # Municipal covers name the state on the commission line
        # ("राज्य निर्वाचन आयोग, राजस्थान") rather than in an "S24" token.
        out["state_name"] = _clean(_first(
            r"राज्य\s*निर्वाचन\s*आयोग\s*[,،:]\s*([^\n]+)", joined))

    ac_line = next((line for line in lines if "विधानसभा" in line and "संसदीय" not in line), "")
    ac_value = ac_line.split(":", 1)[1] if ":" in ac_line else ""
    out["ac_code"], out["ac_name"], out["ac_reservation"] = _split_code_name(ac_value)
    out["part_number"] = _int_or_none(_first(rf"भाग\s*संख्या[ \t:：]*({_D}+)", joined))

    # The colon before the value is the glyph OCR drops most often, so the
    # value is found by its own shape: "<number> - <name> (<reservation>)".
    pc_line = next((line for line in lines if "संसदीय" in line), "")
    pc_hit = None
    for pc_hit in re.finditer(rf"({_D}{{1,3}})\s*[-–—]\s*([^\n(]+?)\s*(?:\(([^)]*)\))?\s*$", pc_line):
        pass
    if pc_hit:
        out["pc_code"], out["pc_name"], out["pc_reservation"] = pc_hit.group(1), _clean(pc_hit.group(2)), _clean(pc_hit.group(3) or "")
    else:
        pc_value = pc_line.rsplit(":", 1)[1] if ":" in pc_line else ""
        out["pc_code"], out["pc_name"], out["pc_reservation"] = _split_code_name(pc_value)

    out["revision_year"] = _int_or_none(_first(rf"(?:{_LABELS['revision_year']}){_SEP}({_D}{{4}})", joined))
    out["qualifying_date"] = _clean(_first(rf"(?:{_LABELS['qualifying_date']}){_SEP}{_DATE}", joined))
    out["publication_date"] = _clean(_first(rf"(?:{_LABELS['publication_date']}){_SEP}{_DATE}", joined))
    if not out["qualifying_date"] or not out["publication_date"]:
        # The labels are often misread; the form prints exactly these two
        # dates in this order, so fall back to position.
        dates = [_clean(d) for d in re.findall(_DATE, joined)]
        if not out["qualifying_date"] and dates:
            out["qualifying_date"] = dates[0]
        if not out["publication_date"] and len(dates) >= 2:
            out["publication_date"] = dates[-1]
    out["revision_type"] = _after_label(_LABELS["revision_type"], joined)

    ident = ""
    for index, line in enumerate(lines):
        hit = re.search(r"नामावली\s*की\s*पहचान", line)
        if hit:
            tail = _clean(line[hit.end():])
            ident = tail or (lines[index + 1] if index + 1 < len(lines) else "")
            break
    out["roll_identification"] = _clean(re.split(r"अर्हता|तिथि\s*:", ident)[0])

    locality_stop = "|".join(_LABELS[key] for key in _LOCALITY_KEYS)
    sections: list[str] = []
    start = next((i for i, line in enumerate(lines) if re.search(r"अनुभाग[ों]*\s*की\s*संख्या", line)), None)
    if start is not None:
        for line in lines[start + 1:start + 12]:
            if re.search(r"मतदान\s*स्थल", line):
                break
            head = re.split(locality_stop, line)[0]
            head = _clean(head)
            if head and not re.fullmatch(r"[\W\d_]+", head):
                sections.append(head)
    out["sections"] = sections

    for key in _LOCALITY_KEYS:
        out[key] = _after_label(_LABELS[key], joined)
    # A colon the OCR turned into a digit lands in front of the PIN; the PIN
    # itself is the last six digits.
    pin_digits = _digits(out["pin_code"])
    out["pin_code"] = pin_digits[-6:] if len(pin_digits) >= 6 else ""

    ps_index = next((i for i, line in enumerate(lines) if re.search(
        r"मतदान\s*(?:स्थल|केंद्र|केन्द्र)\s*की\s*संख्या", line)), None)
    out["polling_station_number"], out["polling_station_name"] = "", ""
    if ps_index is not None:
        for line in lines[ps_index:ps_index + 4]:
            # A lone "1" in the Hindi model often comes out as a bar.
            line = re.sub(r"^[|lI!]\s*(?=[-–—])", "1 ", line.strip())
            match = re.search(rf"(?:^|:\s*)({_D}{{1,4}})\s*[-–—]\s*(.+)", line)
            if match:
                out["polling_station_number"] = match.group(1)
                out["polling_station_name"] = _clean(re.split(r"इस\s*भाग|मतदान\s*स्थल", match.group(2))[0])
                break
    addr_index = next((i for i, line in enumerate(lines) if re.search(r"मतदान\s*स्थल\s*का\s*पता", line)), None)
    out["polling_station_address"] = ""
    if addr_index is not None:
        tail = _clean(lines[addr_index].split(":", 1)[1]) if ":" in lines[addr_index] else ""
        out["polling_station_address"] = tail or (lines[addr_index + 1] if addr_index + 1 < len(lines) else "")
        out["polling_station_address"] = _clean(re.split(r"निर्वाचक[ों]*\s*की\s*संख्या|^\d\.\s", out["polling_station_address"])[0])
    out["polling_station_type"] = _clean(_first(r"सामान्य\s*\)\s*:\s*\|?\s*([^|\n]+)", joined))
    out["auxiliary_stations"] = _int_or_none(_first(rf"सहायक\s*मतदान\s*स्थल[ों]*\s*की\s*\n?\s*संख्या{_SEP}({_D}+)", joined))
    out["total_pages"] = _int_or_none(_first(rf"कुल\s*पृष्ठ[ \t:：]*({_D}+)", joined))
    return out


# ── Scanned page text, with numbers re-read by the digit model ───────────────

_NUMERIC_WORD = re.compile(r"[0-9\u0966-\u096F]{2,}|[0-9\u0966-\u096F]+[-/.][0-9\u0966-\u096F]+")


_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _digit_reads(gray: np.ndarray, box: tuple[int, int, int, int], left_pad: int) -> list[str]:
    """Two independent digit-model reads of one word box."""
    left, top, width, height = box
    pad = 8
    crop = gray[max(0, top - pad):top + height + pad, max(0, left - left_pad):left + width + pad]
    if crop.size == 0:
        return []
    reads = []
    for psm, scale in ((7, max(1.0, 64 / max(1, crop.shape[0]))), (8, 3.0)):
        view = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        view = cv2.copyMakeBorder(view, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
        text = pytesseract.image_to_string(
            view, lang="eng", config=f"--psm {psm} -c tessedit_char_whitelist=0123456789-/.:",
        )
        reads.append(text.strip().strip(".:-/"))
    return reads


def _vote(normalized: str, reads: list[str]) -> str | None:
    candidates = [normalized] + [r for r in reads if r]
    for candidate in candidates:
        if candidates.count(candidate) >= 2:
            return candidate
    want = len(_digits(normalized))
    for read in reads:
        if read and len(_digits(read)) == want:
            return read
    return None


def _best_number(gray: np.ndarray, word: str, box: tuple[int, int, int, int]) -> str:
    """The Hindi read of a number, corrected by the digit model where it can be.

    Agreement between any two of (Hindi read, psm 7, psm 8) wins; failing
    that, a digit read with the Hindi read's digit count wins, because the
    Hindi model gets the count right and the glyphs wrong (07-07 for 01-01).

    Only when both digit reads are *shorter* than the Hindi read is the box
    widened one glyph to the left and tried again: the Hindi model's box for
    "12" regularly covers just the "2". Widening by default is wrong -- it
    pulls the colon before "53" in as a 1.
    """
    normalized = word.translate(_DEVANAGARI_DIGITS)
    reads = _digit_reads(gray, box, left_pad=8)
    # Count only ASCII digits of the original: a Devanagari ५ in "५24" is a
    # misread S, not a third digit to be restored.
    want = len(_digits(word))
    if any(reads) and all(len(_digits(r)) < want for r in reads):
        # The tight box lost a leading glyph ("250" read as "50"). Widen,
        # and trust the wider read only if it restores exactly that count.
        wide = _digit_reads(gray, box, left_pad=box[3])
        chosen = _vote(normalized, wide)
        if chosen is not None and len(_digits(chosen)) == want:
            return chosen
    chosen = _vote(normalized, reads)
    return chosen if chosen is not None else normalized


def ocr_cover_lines(gray: np.ndarray, config: str = "--psm 6") -> list[dict[str, Any]]:
    """Page lines in reading order, each with its vertical extent.

    One hin+eng pass gives the words and where they sit. Every word that is
    mostly digits is then read again from its own box with the English model
    and a digit whitelist: the Hindi model turns 01-01-2026 into 07-07-2026
    and 12 into 72, and those are the fields this page exists to provide.
    """
    data = pytesseract.image_to_data(
        gray, lang="hin+eng", config=config, output_type=pytesseract.Output.DICT,
    )
    lines: dict[tuple[int, int, int], dict[str, Any]] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        box = (int(data["left"][i]), int(data["top"][i]), int(data["width"][i]), int(data["height"][i]))
        if _NUMERIC_WORD.search(word):
            word = _best_number(gray, word, box)
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        line = lines.setdefault(key, {"words": [], "top": box[1], "bottom": box[1] + box[3]})
        line["words"].append((box[0], word))
        line["top"] = min(line["top"], box[1])
        line["bottom"] = max(line["bottom"], box[1] + box[3])
    ordered = []
    for key in sorted(lines, key=lambda k: (k[0], k[1], k[2])):
        line = lines[key]
        ordered.append({
            "text": " ".join(word for _, word in sorted(line["words"])),
            "top": line["top"],
            "bottom": line["bottom"],
        })
    return ordered


def ocr_cover_text(gray: np.ndarray) -> str:
    return "\n".join(line["text"] for line in ocr_cover_lines(gray))


_PS_LABEL = re.compile(r"मतदान\s*स्थल\s*की\s*संख्या")
_COUNTS_LABEL = re.compile(r"निर्वाचकों\s*की\s*संख्या")


def _reread_polling_station(gray: np.ndarray, lines: list[dict[str, Any]], fields: dict[str, Any]) -> None:
    """Second look at section 3 when the page-wide pass lost the station line.

    Tesseract's page layout sometimes merges the "number and name" value with
    the type/address labels beside it and drops the digits. Cropping the band
    between the section-3 label and the section-4 heading and reading it as a
    single column gets the line back.
    """
    if fields.get("polling_station_number"):
        return
    top = next((line["top"] for line in lines if _PS_LABEL.search(line["text"])), None)
    bottom = next((line["top"] for line in lines if _COUNTS_LABEL.search(line["text"])), None)
    if top is None or bottom is None or bottom - top < 20:
        return
    band = gray[max(0, top - 10):bottom, :]
    # Single column first; sparse-text mode when the layout engine still
    # swallows the value into the labels beside it.
    for config in ("--psm 4", "--psm 11"):
        for line in ocr_cover_lines(band, config=config):
            text = re.sub(r"^[|lI!]\s*(?=[-–—])", "1 ", line["text"].strip())
            text = re.sub(r"[:：]\s*[|lI!]\s*(?=[-–—])", ": 1 ", text)
            match = re.search(rf"(?:^|:\s*)({_D}{{1,4}})\s*[-–—]\s*(.+)", text)
            if match and not re.search(r"भाग\s*संख्या|विधानसभा", text):
                fields["polling_station_number"] = match.group(1)
                if not fields.get("polling_station_name"):
                    fields["polling_station_name"] = _clean(
                        re.split(r"इस\s*भाग|मतदान\s*स्थल", match.group(2))[0]
                    )
                fields["polling_station_source"] = f"section_reread:{config}"
                return


# ── Counts table ─────────────────────────────────────────────────────────────

def _numeric_tokens(img: np.ndarray, lang: str, config: str) -> list[dict[str, int]]:
    data = pytesseract.image_to_data(img, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    tokens = []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word or not word.isdigit():
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        tokens.append({
            "text": word, "left": int(data["left"][i]), "top": int(data["top"][i]),
            "width": int(data["width"][i]), "height": int(data["height"][i]), "conf": conf,
        })
    return tokens


def _data_row(tokens: list[dict[str, int]]) -> list[dict[str, int]]:
    """The horizontal run of numbers that is the table's data row."""
    good = [t for t in tokens if t["conf"] >= 40 and 14 <= t["height"] <= 90]
    best: list[dict[str, int]] = []
    for anchor in good:
        row = [t for t in good if abs(t["top"] - anchor["top"]) <= max(12, anchor["height"] // 2)]
        if len(row) > len(best):
            best = row
    return sorted(best, key=lambda t: t["left"])


def _vertical_lines(band: np.ndarray) -> list[int]:
    binary = (band < 140).astype(np.uint8)
    ratio = binary.sum(axis=0) / max(1, band.shape[0])
    columns = np.where(ratio > 0.7)[0]
    lines: list[int] = []
    for x in columns:
        if lines and x - lines[-1] <= 6:
            lines[-1] = x  # extend the run; keep its right edge for now
            continue
        lines.append(int(x))
    return lines


def _read_cell(cell: np.ndarray) -> str:
    if cell.size == 0:
        return ""
    cell = cv2.copyMakeBorder(cell, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    scale = max(1.0, 64 / max(1, cell.shape[0]))
    if scale > 1.0:
        cell = cv2.resize(cell, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    config = "--psm 7 -c tessedit_char_whitelist=0123456789"
    text = _digits(pytesseract.image_to_string(cell, lang="eng", config=config))
    if not text:
        config = "--psm 10 -c tessedit_char_whitelist=0123456789"
        text = _digits(pytesseract.image_to_string(cell, lang="eng", config=config))
    return text


def read_counts_from_image(gray: np.ndarray) -> dict[str, Any]:
    """Section 4 counts from a rendered cover page."""
    height = gray.shape[0]
    y0 = int(height * 0.55)
    band = gray[y0:int(height * 0.95), :]
    tokens = _numeric_tokens(band, "eng", "--psm 11 -c tessedit_char_whitelist=0123456789")
    row = _data_row(tokens)
    if len(row) < 3:
        return {"source": "none", "cells": [], "row_tokens": [t["text"] for t in row]}

    top = int(np.median([t["top"] for t in row]))
    row_h = int(np.median([t["height"] for t in row]))
    ry0 = max(0, top - row_h)
    ry1 = min(band.shape[0], top + 2 * row_h)
    strip = band[ry0:ry1, :]
    lines = _vertical_lines(strip)

    cells: list[str] = []
    boxes: list[tuple[int, int]] = []
    if len(lines) >= 7:
        for left, right in zip(lines, lines[1:]):
            if right - left < 50:
                continue
            boxes.append((left + 6, right - 6))
        for left, right in boxes:
            cells.append(_read_cell(strip[:, left:right]))
        # A separator read as text would shift every column; keep only cells
        # that sit on the data row. Six is the form; trailing empties are
        # outside the table.
        while len(cells) > 6 and cells[-1] == "":
            cells.pop(); boxes.pop()
        while len(cells) > 6 and cells[0] == "":
            cells.pop(0); boxes.pop(0)

    source = "cells"
    if len(cells) != 6:
        # Fall back to the tokens the whole-band pass found, in x order.
        cells = [t["text"] for t in row]
        source = "tokens"

    out: dict[str, Any] = {"source": source, "cells": cells, "row_tokens": [t["text"] for t in row]}
    if len(cells) == 6:
        for key, value in zip(COUNT_COLUMNS, cells):
            out[key] = int(value) if value else None
    return out


def read_counts_from_text(page: fitz.Page) -> dict[str, Any]:
    """Section 4 counts from a text-layer page: the numeric row below the labels."""
    words = page.get_text("words")  # x0, y0, x1, y1, text, block, line, word
    label = [w for w in words if "महिला" in w[4]]
    if not label:
        return {"source": "none", "cells": []}
    label_y = max(w[3] for w in label)
    rows: dict[int, list] = {}
    for w in words:
        if w[1] > label_y and w[4].strip().isdigit():
            rows.setdefault(int(w[1] // 4), []).append(w)
    best = max(rows.values(), key=len, default=[])
    cells = [w[4] for w in sorted(best, key=lambda w: w[0])]
    out: dict[str, Any] = {"source": "text", "cells": cells}
    if len(cells) == 6:
        for key, value in zip(COUNT_COLUMNS, cells):
            out[key] = int(value)
    return out


def _render_gray(page: fitz.Page, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


_FILENAME_PART = re.compile(r"-(\d{1,4})-WI\.pdf$", re.IGNORECASE)


def _reconcile_part_number(fields: dict[str, Any], filename: str | None) -> None:
    """Check the OCR'd part number against the one in the ECI file name.

    ECI names each roll "...-HIN-<part>-WI.pdf", so when the name carries a
    part number it is the more reliable of the two: the OCR value is a short
    number next to a colon, which the Hindi model misreads (": 512" as "1512",
    ": 1" as "7"). The OCR value is kept when they agree; otherwise the file
    name wins and the cover's reading is kept alongside for the record.
    Files not named by ECI keep whatever the cover said.
    """
    fields["part_number_source"] = "cover"
    match = _FILENAME_PART.search(filename or "")
    if not match:
        return
    from_name = int(match.group(1))
    fields["part_number_in_filename"] = from_name
    read = fields.get("part_number")
    if read == from_name:
        return
    fields["part_number_on_cover"] = read
    fields["part_number"] = from_name
    fields["part_number_source"] = "filename"


def _eci_cover_labels(text: str) -> bool:
    """True when the text is the ECI form, not a font that stored Hindi wrong.

    A municipal roll printed by iText stores the digits exactly and the
    Devanagari with its matras destroyed, so the long text layer looks
    readable and contains none of the words the ECI parser looks for.
    """
    return bool(re.search(r"विधान\s*सभा|महिला|भाग\s*संख्या", text or ""))


def _exact_cover_numbers(page: fitz.Page) -> dict[str, Any]:
    """Digits the text layer stored correctly, located by shape not by label.

    The labels are unreadable in this encoding. The numbers are not: the
    constituency is the token ``46-…`` in the header, the ward and the part
    are the only header line with two integers far apart, the counts are the
    only line of six integers, and the polling station is the integer that
    is followed by a dash.
    """
    words = page.get_text("words") or []
    height = float(page.rect.height) or 1.0
    rows: dict[int, list] = {}
    for word in words:
        rows.setdefault(int(round(word[1] / 3.0) * 3), []).append(word)
    found: dict[str, Any] = {}
    for y, group in rows.items():
        group = sorted(group, key=lambda word: word[0])
        ints = [word for word in group if re.fullmatch(r"\d{1,6}", word[4] or "")]
        if len(ints) == 6 and y > height * 0.55:
            for key, word in zip(COUNT_COLUMNS, ints):
                found[key] = int(word[4])
        if (y < height * 0.30 and len(ints) == 2
                and ints[1][0] - ints[0][0] > 100):
            found.setdefault("ward", ints[0][4])
            found.setdefault("part_number", int(ints[1][4]))
        for word in group:
            # The dash must be followed by a letter. A date ("01-01-2026")
            # also sits in the header and would otherwise become the code.
            if y < height * 0.30 and re.match(r"^\d{1,4}[-–—](?!\d)", word[4] or ""):
                found["ac_code"] = re.match(r"^(\d{1,4})", word[4]).group(1)
                found["ac_band"] = (float(word[1]) - 2.0, float(word[3]) + 2.0)
        if height * 0.45 < y < height * 0.85 and "polling_station_number" not in found:
            for index, word in enumerate(group):
                nxt = group[index + 1][4] if index + 1 < len(group) else ""
                if re.fullmatch(r"\d{2,4}", word[4] or "") and nxt in {"-", "–", "—"}:
                    found["polling_station_number"] = word[4]
                    found["ps_band"] = (float(word[1]) - 3.0, float(word[3]) + 3.0)
                    rooms = [later[4] for later in group[index + 2:]
                             if re.fullmatch(r"\d+", later[4] or "")]
                    if rooms:
                        found["ps_room"] = rooms[-1]
                    break
    return found


def _ocr_band(page: fitz.Page, y0: float, y1: float) -> str:
    """Read one printed line. The text layer's Hindi is wrong; the glyphs are not."""
    clip = fitz.Rect(page.rect.x0, max(0.0, y0), page.rect.x1, min(page.rect.height, y1))
    if clip.height < 2:
        return ""
    pix = page.get_pixmap(dpi=300, clip=clip, colorspace=fitz.csGRAY)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    text = pytesseract.image_to_string(image, lang="hin+eng", config="--oem 3 --psm 7")
    return _clean(text.replace("\n", " "))


def _apply_exact_text_layer(page: fitz.Page, fields: dict[str, Any], counts: dict[str, Any]) -> None:
    """Prefer the text layer's digits and the rendered line's Hindi names."""
    exact = _exact_cover_numbers(page)
    if exact.get("ward"):
        fields["ward"] = exact["ward"]
    if exact.get("part_number") is not None:
        fields["part_number"] = exact["part_number"]
    if exact.get("ac_code"):
        fields["ac_code"] = exact["ac_code"]
    for key in COUNT_COLUMNS:
        if exact.get(key) is not None:
            counts[key] = exact[key]
    if exact.get("polling_station_number"):
        fields["polling_station_number"] = exact["polling_station_number"]
    band = exact.get("ac_band")
    if band and not fields.get("ac_name"):
        read = _ocr_band(page, band[0], band[1])
        match = re.search(rf"{re.escape(str(exact.get('ac_code', '')))}\s*[-–—:]\s*(.+)", read)
        name = _clean(re.split(r"वार्ड|भाग", match.group(1))[0]) if match else ""
        if re.search(r"[\u0900-\u097F]", name):
            fields["ac_name"] = name
    ps_band = exact.get("ps_band")
    if ps_band:
        read = _ocr_band(page, ps_band[0], ps_band[1])
        match = re.search(r"\d{2,4}\s*[-–—]\s*(.+)", read)
        name = _clean(match.group(1)) if match else ""
        # A Latin scrap ("are", "Pega") is the Hindi model giving up on one
        # word, not part of the station name.
        name = _clean(re.sub(r"\b[A-Za-z]{2,}\b", " ", name))
        room = exact.get("ps_room")
        if name and room and re.search(r"\d+\s*$", name) and not name.endswith(str(room)):
            name = re.sub(r"\d+\s*$", str(room), name)
        if name:
            fields["polling_station_name"] = name


def extract_cover(pdf_bytes: bytes, filename: str | None = None) -> dict[str, Any]:
    """Everything the cover page says, plus the printed counts."""
    started = time.perf_counter()
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if len(document) == 0:
            raise ValueError("PDF has no pages")
        page = document[0]
        page_count = len(document)
        text = page.get_text() or ""
        # A long text layer whose Hindi labels survived is the ECI form and
        # is parsed as text. The same length with no labels is a font that
        # stored the letters wrong: render the page for the names, then put
        # the text layer's exact digits back on top.
        labels_ok = _eci_cover_labels(text)
        if len(text.strip()) >= TEXT_LAYER_MIN_CHARS and labels_ok:
            source = "text_layer"
            raw = text
            counts = read_counts_from_text(page)
            gray = lines = None
        else:
            source = "render" if len(text.strip()) >= TEXT_LAYER_MIN_CHARS else "tesseract"
            gray = _render_gray(page, COVER_RENDER_DPI)
            lines = ocr_cover_lines(gray)
            raw = "\n".join(line["text"] for line in lines)
            counts = read_counts_from_image(gray)

        fields = parse_cover_text(raw)
        if source == "tesseract" and gray is not None and lines is not None:
            _reread_polling_station(gray, lines, fields)
        if source == "render":
            # While the page is still open: the name crops are taken from it.
            _apply_exact_text_layer(page, fields, counts)
    _reconcile_part_number(fields, filename)
    totals = {key: counts.get(key) for key in COUNT_COLUMNS}
    parts = [totals.get("male"), totals.get("female"), totals.get("third_gender")]
    consistent = None
    if totals.get("total") is not None and all(p is not None for p in parts):
        consistent = sum(parts) == totals["total"]

    return {
        "source": source,
        "page_count": page_count,
        "fields": fields,
        "counts": totals,
        "counts_consistent": consistent,
        "counts_debug": {k: v for k, v in counts.items() if k not in COUNT_COLUMNS},
        "raw_text": raw,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
