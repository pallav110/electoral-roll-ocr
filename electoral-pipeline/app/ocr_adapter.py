"""Read a voter card by the labels printed on it.

The two measured readers know one layout each. This one does not. It takes
the card box the gate drew around a voter ID and keeps the text that follows
a label: नाम / Name, पिता का नाम / Father's Name, मकान संख्या / House Number,
आयु / Age, लिंग / Gender, and the husband, mother, and other relation labels.

A card with no such label is left unread. Guessing which bare word is the
name would store the wrong column, which is the failure this file exists to
avoid.

Fine-tuning a model on corrected rows is a later step. It needs those
corrections as examples first. This file does not train anything; it is the
reader that produces the first rows an operator can correct.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Longest-first is not enough: "Father's Name" contains "Name". Each search
# takes the match that starts earliest, and these patterns are listed so a
# relation label is tried as its own phrase.
_LABELS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfather'?s?\s+name|\bfather\s+name", re.I), "father"),
    (re.compile(r"\bhusband'?s?\s+name|\bhusband\s+name", re.I), "husband"),
    (re.compile(r"\bmother'?s?\s+name|\bmother\s+name", re.I), "mother"),
    (re.compile(r"पिता\s*का\s*नाम|पिता"), "father"),
    (re.compile(r"पति\s*का\s*नाम|पति"), "husband"),
    (re.compile(r"माता\s*का\s*नाम|माता"), "mother"),
    (re.compile(r"अन्य\s*का\s*नाम|अन्य"), "other"),
    (re.compile(r"मकान\s*संख्या|मकान|\bhouse\s*(?:no\.?|number)?", re.I), "house"),
    (re.compile(r"आयु|\bage\b", re.I), "age"),
    (re.compile(r"लिंग|\bgender\b|\bsex\b", re.I), "gender"),
    (re.compile(
        r"क्रमांक|क्रम\s*संख्या|\bserial(?:\s*no\.?)?|\bs\.?\s*no\.?", re.I), "serial"),
    (re.compile(r"नाम|\bname\b", re.I), "name"),
)

_RELATION_WORD = {
    "father": "पिता",
    "husband": "पति",
    "mother": "माता",
    "other": "अन्य",
}
_RELATION_FIELD = {
    "father": "voter_father_first_name",
    "husband": "voter_husband_first_name",
    "mother": "voter_mother_first_name",
    "other": "voter_other_first_name",
}

_GENDER = (
    (re.compile(r"तृतीय|third", re.I), "तृतीय लिंग"),
    (re.compile(r"महिला|female|\bf\b", re.I), "महिला"),
    (re.compile(r"पुरुष|male|\bm\b", re.I), "पुरुष"),
)


@dataclass
class AdapterResult:
    """``records`` is empty for a cover page. ``unlabeled`` means stop.

    ``anchors`` is one dict per named card: field name to its offset from
    the voter ID. The template writer uses it. The readers do not.
    """

    status: str
    records: list[dict] = field(default_factory=list)
    anchors: list[dict] = field(default_factory=list)
    page_size: tuple[float, float] = (0.0, 0.0)


def ocr_page_words(page) -> list[tuple]:
    """Words on a scanned page, in PDF points, shaped like PyMuPDF words.

    An empty list means there is no OCR engine or the page produced nothing.
    The caller then leaves the file on the scanned reader.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("tesseract") is None:
        return []
    dpi = 200
    scale = 72.0 / dpi
    pixmap = page.get_pixmap(dpi=dpi)
    with tempfile.TemporaryDirectory() as tmp:
        image = f"{tmp}/page.png"
        pixmap.save(image)
        try:
            completed = subprocess.run(
                ["tesseract", image, "stdout", "-l", "hin+eng", "--psm", "6", "tsv"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
    words = []
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            left, top, width, height = (float(parts[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        x0 = left * scale
        y0 = top * scale
        words.append((x0, y0, x0 + width * scale, y0 + height * scale, text))
    return words


def read_pages(path: str, page_from: int, page_to: int) -> AdapterResult:
    """Read one page range. Pages are numbered from 1, as the pipeline sends them."""
    import fitz

    document = fitz.open(path)
    try:
        records: list[dict] = []
        ids_seen = 0
        anchors: list[dict] = []
        page_size = (0.0, 0.0)
        last = min(page_to, document.page_count)
        first = max(1, page_from)
        for number in range(first, last + 1):
            page = document[number - 1]
            page_size = (round(page.rect.width, 1), round(page.rect.height, 1))
            page_records, page_ids, page_anchors = _read_page(page, number)
            ids_seen += page_ids
            records.extend(page_records)
            anchors.extend(page_anchors)
        if ids_seen == 0:
            return AdapterResult("no-cards")
        # A page of IDs whose boxes carry no name is not a layout we can read.
        # Returning the few that happened to match a word would hide the rest.
        named = sum(1 for record in records if record.get("voter_first_name"))
        if named == 0 or named * 2 < ids_seen:
            return AdapterResult("unlabeled")
        return AdapterResult("records", records, anchors, page_size)
    finally:
        document.close()


def _read_page(page, page_number: int) -> tuple[list[dict], int, list[dict]]:
    from app.roll_gate import _boxes_for_page, _id_token

    words = page.get_text("words") or []
    boxes = _boxes_for_page(page, page_number, min_ids=1, words=words)
    if not boxes:
        words = ocr_page_words(page)
        boxes = _boxes_for_page(page, page_number, min_ids=1, words=words)
    if not boxes:
        return [], 0, []
    records = []
    anchors = []
    for index, box in enumerate(boxes):
        lines = _lines_in_box(words, box)
        fields = _fields_from_lines(lines)
        if not fields.get("name"):
            continue
        records.append(_record(box, index, page_number, fields))
        offsets = _label_offsets(words, box, _id_token)
        if offsets:
            offsets["card_width"] = box["width"]
            offsets["card_height"] = box["height"]
            offsets["card_x"] = box["x"]
            anchors.append(offsets)
    return records, len(boxes), anchors


def _label_offsets(words: list, box: dict, id_token) -> dict[str, dict]:
    """Where each printed label sits, measured from the voter ID.

    Age and sex are taken from the label word, not from the number or the
    letter beside it. A bare "45" is not an anchor.
    """
    chosen = _words_in_box(words, box)
    epic = next((word for word in chosen if id_token(word[4]) == box["id"]), None)
    if epic is None:
        return {}
    offsets: dict[str, dict] = {}
    for line in _word_lines(chosen):
        text = " ".join(word[4].strip() for word in line if word[4].strip())
        cursor = 0
        while cursor < len(text):
            match = _earliest_label(text, cursor)
            if match is None:
                break
            start, end, kind = match
            word = _word_at_char(line, text, start)
            if word is not None and kind not in offsets:
                offsets[kind] = {
                    "dx": round(word[0] - epic[0], 1),
                    "dy": round(word[1] - epic[1], 1),
                }
            cursor = end if end > cursor else cursor + 1
    return offsets


def _words_in_box(words: list, box: dict) -> list:
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["width"], y0 + box["height"]
    return [
        word for word in words
        if x0 <= (word[0] + word[2]) / 2 <= x1 and y0 <= (word[1] + word[3]) / 2 <= y1
    ]


def _word_lines(words: list) -> list[list]:
    ordered = sorted(words, key=lambda word: (word[1], word[0]))
    lines: list[list] = []
    for word in ordered:
        if not lines or abs(word[1] - lines[-1][0][1]) > 8:
            lines.append([word])
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda word: word[0])
    return lines


def _word_at_char(line: list, text: str, char_index: int):
    cursor = 0
    for word in line:
        token = word[4].strip()
        if not token:
            continue
        start = text.find(token, cursor)
        if start < 0:
            continue
        if start <= char_index < start + len(token):
            return word
        cursor = start + len(token)
    return None


def _lines_in_box(words: list, box: dict) -> list[str]:
    x0 = box["x"]
    y0 = box["y"]
    x1 = x0 + box["width"]
    y1 = y0 + box["height"]
    chosen = []
    for word in words:
        cx = (word[0] + word[2]) / 2
        cy = (word[1] + word[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            chosen.append(word)
    chosen.sort(key=lambda word: (word[1], word[0]))
    lines: list[list] = []
    for word in chosen:
        if not lines or abs(word[1] - lines[-1][0][1]) > 8:
            lines.append([word])
        else:
            lines[-1].append(word)
    text_lines = []
    for line in lines:
        line.sort(key=lambda word: word[0])
        text_lines.append(" ".join(word[4].strip() for word in line if word[4].strip()))
    return [line for line in text_lines if line]


def _fields_from_lines(lines: list[str]) -> dict[str, str]:
    """First value wins. A later repeat is usually the next card's leak."""
    found: dict[str, str] = {}
    for line in lines:
        cursor = 0
        text = unicodedata.normalize("NFC", line)
        while cursor < len(text):
            match = _earliest_label(text, cursor)
            if match is None:
                break
            start, end, kind = match
            nxt = _earliest_label(text, end)
            value_end = nxt[0] if nxt else len(text)
            value = _clean(text[end:value_end])
            if value and kind not in found:
                found[kind] = value
            cursor = end if value_end == end else value_end
    return found


def _earliest_label(text: str, start: int) -> tuple[int, int, str] | None:
    best = None
    for pattern, kind in _LABELS:
        match = pattern.search(text, start)
        if match is None:
            continue
        if best is None or match.start() < best[0]:
            best = (match.start(), match.end(), kind)
    return best


def _clean(value: str) -> str:
    return re.sub(r"^[\s:.\-]+", "", value).strip(" .;|-")


def _record(box: dict, index: int, page_number: int, fields: dict) -> dict:
    first, middle, sur = _split_name(fields.get("name", ""))
    record = {
        "page_number": page_number,
        "card_index": index,
        "id_card_no": box["id"],
        "voter_first_name": first,
        "voter_middle_name": middle,
        "voter_sur_name": sur,
        "house_no": fields.get("house", ""),
        "age": _age(fields.get("age", "")),
        "gender": _gender(fields.get("gender", "")),
        "voter_sr_no": _serial(fields.get("serial", "")),
        "confidence": 0.8 if fields.get("house") or fields.get("age") else 0.6,
        "field_sources": {"reader": "ocr_adapter"},
    }
    for kind, column in _RELATION_FIELD.items():
        if fields.get(kind):
            record["relation_name"] = _RELATION_WORD[kind]
            record[column] = fields[kind]
            break
    return record


def _split_name(name: str) -> tuple[str, str, str]:
    parts = [part for part in name.split() if part]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def _age(value: str) -> str:
    match = re.search(r"\d{1,3}", value)
    if not match:
        return ""
    number = int(match.group())
    if 1 <= number <= 120:
        return str(number)
    return ""


def _gender(value: str) -> str:
    for pattern, canonical in _GENDER:
        if pattern.search(value):
            return canonical
    return ""


def _serial(value: str) -> str:
    match = re.search(r"\d{1,6}", value)
    return match.group() if match else ""
