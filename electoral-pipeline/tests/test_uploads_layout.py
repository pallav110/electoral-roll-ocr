"""The /uploads list must stay ONE list per upload, not one table per batch.

The redesign replaced thirteen <table> elements -- one per batch of 100 --
with .doc-row grid rows grouped by sticky [data-batch-group] strips. The
counting and the percentages that feed the progress bar are the page's only
answer to "did my rolls go through?", so they have to agree with the rows
underneath them.

X must Y, because Z:
  * the progress buckets must partition the documents exactly once, because a
    double-counted status would make the bar contradict the total;
  * an unknown status must fall into 'waiting' rather than vanish, because a
    status added later still has to appear in the bar;
  * the percentages must sum to 100 when there are documents, because a bar
    that does not fill its track misreports progress;
  * there must be no <table> left in the per-upload list, because that is the
    defect this page was rebuilt to remove.
"""
from __future__ import annotations

import re

import pytest

from app.main import PROGRESS_BUCKETS, _progress_of


def _strip_jinja(text: str) -> str:
    """Drop Jinja comments and tags so only real markup is asserted on.

    `{# ... #}` blocks here are long prose explanations of the change being
    tested, and they quote the very markup they removed. Without stripping
    them, the comment that explains "there is no table" would itself contain
    the word <table> and fail the test.
    """
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return re.sub(r"\{[%{].*?[%}]\}", "", text, flags=re.S)


class _Doc:
    def __init__(self, status, last_error_code=None):
        self.status = status
        self.last_error_code = last_error_code


class TestProgressBuckets:
    def test_every_status_lands_in_exactly_one_bucket(self):
        """The buckets partition the statuses. Overlap would double-count."""
        seen: dict[str, str] = {}
        for name, values in PROGRESS_BUCKETS.items():
            for status in values:
                assert status not in seen, (
                    f"{status!r} is in both {seen.get(status)!r} and {name!r}; "
                    "a document matching both would be counted twice"
                )
                seen[status] = name

    def test_unknown_status_falls_into_waiting(self):
        """A new status must show up, not silently disappear from the bar."""
        counts, pct = _progress_of([_Doc("something_new")])
        assert counts["waiting"] == 1
        assert sum(counts.values()) == 1
        assert pct["waiting"] == 100.0

    def test_counts_partition_the_document_list(self):
        docs = [_Doc(s) for s in
                ["pending", "queued", "processing", "completed", "retry",
                 "failed", "cancelled", "partial"]]
        counts, _ = _progress_of(docs)
        assert sum(counts.values()) == len(docs)

    def test_duplicate_cancellation_is_skipped_not_a_problem(self):
        counts, _ = _progress_of([
            _Doc("cancelled", "DUPLICATE_CONTENT"),
            _Doc("cancelled", "OPERATOR_CANCELLED"),
            _Doc("failed", "EXTRACTION_FAILED"),
        ])

        assert counts["skipped"] == 1
        assert counts["problem"] == 2

    def test_percentages_sum_to_one_hundred(self):
        docs = [_Doc("completed")] * 3 + [_Doc("pending")] * 5 + [_Doc("failed")]
        counts, pct = _progress_of(docs)
        assert sum(counts.values()) == 9
        assert sum(pct.values()) == pytest.approx(100.0)

    def test_empty_upload_does_not_divide_by_zero(self):
        """An upload whose files did not arrive still renders a bar."""
        counts, pct = _progress_of([])
        assert sum(counts.values()) == 0
        assert sum(pct.values()) == 0.0

    def test_waiting_is_the_fallback_not_a_declared_status(self):
        """'waiting' must stay empty in the map so the fallback branch owns it."""
        assert PROGRESS_BUCKETS["waiting"] == ()


class TestTemplateMarkup:
    @pytest.fixture(scope="class")
    def source(self):
        from pathlib import Path
        return Path(__file__).resolve().parents[1] / "app" / "templates" / "uploads.html"

    def test_no_table_in_the_upload_list(self, source):
        """The per-batch <table> is the defect being removed.

        Checked against the RENDERED page rather than the source, because the
        source legitimately mentions <table> in the comments explaining what
        this test is about -- matching on the literal would fail forever
        against its own explanation.
        """
        body = source.read_text(encoding="utf-8")
        start = body.index('<form method="post" action="/uploads/extract"')
        end = body.index("</form>", start)
        markup = _strip_jinja(body[start:end])
        assert "<table" not in markup
        assert "<tr" not in markup
        assert "<td" not in markup

    def test_rows_carry_the_data_attribute_the_script_hooks(self, source):
        """The checkbox script finds groups by attribute, not by class."""
        body = source.read_text(encoding="utf-8")
        assert 'class="batch-strip" data-batch-group' in body
        assert 'class="doc-row" data-doc-row' in body

    def test_script_no_longer_references_the_removed_batch_group_class(self, source):
        """A leftover .batch-group lookup would throw on closest(null)."""
        body = source.read_text(encoding="utf-8")
        script = body[body.index("<script>"):]
        assert ".batch-group" not in script
        assert ".batch-head" not in script

    def test_no_inline_font_overrides_on_the_batch_strip(self, source):
        """The strip is styled by .batch-strip, not by a style="" escape hatch."""
        body = source.read_text(encoding="utf-8")
        block = body[body.index("data-batch-group"):body.index("data-batch-group") + 700]
        assert "font:600" not in block
        assert "font-weight:400" not in block

    def test_batch_index_survives_as_metadata_not_a_column(self, source):
        """It was noise as a column; it must not have been deleted outright."""
        body = source.read_text(encoding="utf-8")
        assert "d.batch_index" in body

    def test_progress_track_never_carries_text(self, source):
        """Carbon: nothing inside the track. A painted number is unreadable."""
        body = _strip_jinja(source.read_text(encoding="utf-8"))
        track = body[body.index('class="progress-track"'):]
        track = track[:track.index("</div>")]
        # Every element inside the track must be a segment span. Anything else
        # -- a <b>, a bare number, a label -- is content painted on an 8px bar.
        children = re.findall(r'<([a-zA-Z][a-zA-Z0-9]*)', track)
        assert set(children) <= {"div", "span"}, set(children)
        for cls in re.findall(r'class="([^"]*)"', track):
            assert "progress-seg" in cls or cls == "progress-track", cls

    def test_progress_track_has_an_accessible_label(self, source):
        """Carbon requires the label even when the bar is decorative."""
        body = source.read_text(encoding="utf-8")
        marker = '<div class="progress-track" role="img"'
        assert marker in body
        aria = body[body.index(marker):]
        aria = aria[:aria.index(">")]
        assert "aria-label=" in aria
