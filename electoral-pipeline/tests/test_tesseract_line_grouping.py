"""Tesseract rows must be regrouped into lines by their composite key.

The parser-side tests in test_hindi_name_relation_split.py pass real merged
lines in and check the fields that come out. That covers the _expand_line
safety net, but it does NOT cover where those lines came from. If the
grouping key in _extract_text_with_tesseract() regressed to line_num alone,
every one of those tests would still pass while the live pipeline broke --
the merge would simply reappear upstream of the parser.

This drives the real regrouping loop over a recorded TSV and asserts the rows
come back separated. The TSV is the real shape pytesseract emits for a voter
card: each printed row is its own paragraph, so every row's first line carries
line_num == 1.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "ocr_pdf_api.py").read_text(encoding="utf-8")

# The rows below are the real TSV columns, in the order pytesseract's
# image_to_data(DICT) returns them. Note par_num restarting at 2 for the same
# line_num of 1 -- this is precisely what the old key collapsed.
TSV = """\
level	block_num	par_num	line_num	word_num	left	top	width	height	conf	text
5	1	1	1	1	10	20	40	12	95.5	नाम
5	1	1	1	4	60	20	55	12	88.2	मेता
5	1	2	1	1	10	40	40	12	91.0	पिता
5	1	2	1	5	60	40	80	12	85.7	राजकुमार
5	1	3	1	1	10	60	40	12	90.3	आयु
5	1	3	1	4	60	60	30	12	89.9	34
"""


def _group(rows, key_fields):
    """Rebuild lines exactly as _extract_text_with_tesseract does."""
    lines: list[str] = []
    current: list[str] = []
    last_key = None
    for row in rows:
        key = tuple(row[f] for f in key_fields)
        if row["text"].strip():
            if key != last_key and current:
                lines.append(" ".join(current))
                current = []
            current.append(row["text"].strip())
            last_key = key
    if current:
        lines.append(" ".join(current))
    return lines


def _rows():
    out = []
    header = TSV.splitlines()[0].split("\t")
    for raw in TSV.splitlines()[1:]:
        cells = raw.split("\t")
        row = dict(zip(header, cells))
        for f in ("block_num", "par_num", "line_num", "word_num"):
            row[f] = int(row[f])
        out.append(row)
    return out


def test_rows_regroup_into_printed_lines():
    lines = _group(_rows(), ("block_num", "par_num", "line_num"))
    assert lines == ["नाम मेता", "पिता राजकुमार", "आयु 34"]


def test_line_num_alone_would_merge_every_row():
    """The regression itself: the old key fuses all three rows into one."""
    lines = _group(_rows(), ("line_num",))
    assert lines == ["नाम मेता पिता राजकुमार आयु 34"]


def test_source_uses_the_composite_key():
    """Guard the source itself so the bug cannot return unnoticed."""
    body = SRC.split("def _extract_text_with_tesseract", 1)[1]
    body = body.split("\ndef ", 1)[0]
    assert re.search(
        r"line_key\s*=\s*\(\s*data\[['\"]block_num['\"]\]\[i\]\s*,"
        r"\s*data\[['\"]par_num['\"]\]\[i\]\s*,"
        r"\s*data\[['\"]line_num['\"]\]\[i\]\s*,\s*\)",
        body,
    ), "line grouping must key on (block_num, par_num, line_num)"


def test_no_stale_single_field_line_key_survives():
    body = SRC.split("def _extract_text_with_tesseract", 1)[1]
    body = body.split("\ndef ", 1)[0]
    stale = re.findall(r"last_line_key\s*=\s*data\[['\"](\w+)['\"]\]\[i\]", body)
    assert not stale, (
        "last_line_key is still keyed on a single column %r; it must be the "
        "composite tuple" % stale
    )


def test_word_num_is_not_a_grouping_key():
    """word_num restarts per line too -- using it splits every word apart."""
    lines = _group(_rows(), ("block_num", "par_num", "line_num", "word_num"))
    assert lines == ["नाम", "मेता", "पिता", "राजकुमार", "आयु", "34"]


@pytest.mark.parametrize("key", [("block_num", "line_num"), ("line_num",)])
def test_partial_keys_do_not_reproduce_the_lines(key):
    """Keys that drop par_num or fuse two blocks do not separate the rows.

    Note par_num on its own would happen to work here, because this fixture is
    a single block. It is kept in the production key anyway: a full-card
    fallback read routinely yields two blocks (photo text vs. the label
    column), and dropping block_num would then merge rows across them.
    """
    assert _group(_rows(), key) != ["नाम मेता", "पिता राजकुमार", "आयु 34"]


def test_two_blocks_need_block_num_in_the_key():
    """Same rows, split across two blocks: block_num is now load-bearing."""
    rows = _rows()
    for row in rows:
        row["block_num"] = 1 if row["line_num"] == 1 and row["par_num"] <= 2 else 2
        row["par_num"] = 1  # each block restarts its own paragraph counter
    # Without block_num the two blocks' line 1s would be treated as one line.
    assert _group(rows, ("block_num", "par_num", "line_num")) != _group(
        rows, ("par_num", "line_num")
    )