"""Can the OCR card counter be shown on a session page without lying?

The card counter in ocr_pdf_api.py is process-global: it tracks whichever
extraction the single OCR server is running, and it knows nothing about
sessions. The session page knows about sessions and nothing about cards.
`fuse_progress` is the only place the two are combined, so every way that
combination can be wrong is tested here.

The failure that matters is not a crash. It is a page showing another
session's climbing card count, or a finished counter still ticking, or a
blank lattice slot counted as a voter record. Each of those is a confident,
specific, wrong number on an operator's screen.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.presentation import fuse_progress, live_progress


class Unit:
    """An ExtractionUnit as the presentation layer sees one."""

    def __init__(self, number, status, page_from, page_to, records=0,
                 started=None, completed=None):
        self.unit_number = number
        self.status = status
        self.page_from = page_from
        self.page_to = page_to
        self.records_extracted = records
        self.started_at = started
        self.completed_at = completed


def at(seconds_from_start):
    return datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds_from_start)


def scan(**kwargs):
    """An OCR snapshot. Defaults describe a live run 40 cards into page 4."""
    payload = {
        "cards_done": 40, "cards_total": 90, "cards_records": 38,
        "pages_done": 3, "pages_total": 20, "active_page": 4,
        "done": False, "started_at": 1.0, "updated_at": 41.0, "idle_s": 1.5,
    }
    payload.update(kwargs)
    return payload


def progress_for(units, pages_total=20, when=None):
    return live_progress(units, pages_total=pages_total, at=when)


class TestTheLiveMerge:
    def test_a_matching_live_counter_is_used(self):
        units = [Unit(1, "processing", 3, 12)]
        p = fuse_progress(progress_for(units), scan(), page_from=3, page_to=12)

        assert p["cards_live"] is True
        assert p["cards_done"] == 40
        assert p["cards_records"] == 38
        assert p["records_headline"] == 38

    def test_the_headline_prefers_a_real_count_over_a_projection(self):
        """A counted number must beat a projected one even when the projection
        happens to be larger. Both are 'progress'; only one of them happened."""
        units = [
            Unit(1, "completed", 3, 12, records=290, started=at(0), completed=at(600)),
            Unit(2, "processing", 13, 22, started=at(610)),
        ]
        p = fuse_progress(progress_for(units, when=at(700)),
                          scan(cards_records=5, active_page=13),
                          page_from=13, page_to=22)

        assert p["can_estimate"] is True
        assert p["records_estimate"] is not None
        assert p["cards_live"] is True
        assert p["records_headline"] == 5, "a counted 5 must win over a projected ~150"
        assert p["headline_is_estimate"] is False

    def test_without_a_live_counter_the_old_numbers_still_work(self):
        """The database numbers are the fallback and must survive untouched."""
        units = [
            Unit(1, "completed", 3, 12, records=290, started=at(0), completed=at(600)),
            Unit(2, "processing", 13, 22, started=at(610)),
        ]
        base = progress_for(units, when=at(700))
        p = fuse_progress(base, None, page_from=13, page_to=22)

        assert p["cards_live"] is False
        assert p["records_settled"] == base["records_settled"]
        assert p["records_estimate"] == base["records_estimate"]
        assert p["records_headline"] == base["records_estimate"]
        assert p["headline_is_estimate"] is True


class TestRefusingAnotherSessionsCards:
    """The core safety property. The counter is global; the page is not."""

    def test_a_page_outside_the_running_units_range_is_rejected(self):
        units = [Unit(1, "processing", 3, 12)]
        # OCR is reading page 19 -- some other session's unit.
        p = fuse_progress(progress_for(units), scan(active_page=19),
                          page_from=3, page_to=12)

        assert p["cards_live"] is False
        assert p["cards_done"] == 0
        assert p["records_headline"] != 38

    def test_a_counter_with_no_running_unit_is_not_attributed(self):
        """All units still queued: there is no range to test against, so the
        counter must not be borrowed."""
        units = [Unit(1, "pending", 3, 12), Unit(2, "pending", 13, 22)]
        p = fuse_progress(progress_for(units), scan(active_page=5))

        assert p["cards_live"] is False
        assert p["cards_done"] == 0

    def test_a_stale_counter_from_a_finished_request_is_rejected(self):
        """`done` alone is not sufficient -- see the between-units note."""
        units = [Unit(1, "processing", 3, 12)]
        stale = scan(done=True, active_page=4)
        p = fuse_progress(progress_for(units), stale, page_from=3, page_to=12)

        assert p["cards_live"] is False

    def test_an_idle_counter_is_rejected(self):
        """Between two units _OCR_LOCK releases and no OCR work happens. The
        counter still names the previous page, which is inside the previous
        unit's range -- so only the timestamp can reject it."""
        units = [Unit(1, "processing", 13, 22)]
        leftover = scan(active_page=12, idle_s=95.0)
        p = fuse_progress(progress_for(units), leftover, page_from=13, page_to=22)

        assert p["cards_live"] is False, "a 95-second-old counter is not live work"

    def test_a_counter_never_used_is_rejected(self):
        units = [Unit(1, "processing", 3, 12)]
        cold = scan(cards_done=0, cards_total=0, cards_records=0,
                    pages_done=0, active_page=None, idle_s=0.0, started_at=0.0)
        p = fuse_progress(progress_for(units), cold, page_from=3, page_to=12)

        assert p["cards_live"] is False


class TestBlankLatticeSlots:
    """`cards_done` counts grid positions; `cards_records` counts records.

    They are not the same number and conflating them is the one mistake this
    display cannot make: _voter_card_rects() returns 30 positions per page
    whether or not the cell is filled, and page 22 of the real roll yields 30
    cards and 0 records.
    """

    def test_cards_and_records_are_reported_separately(self):
        units = [Unit(1, "processing", 3, 12)]
        p = fuse_progress(progress_for(units), scan(cards_done=40, cards_records=38),
                          page_from=3, page_to=12)

        assert p["cards_done"] == 40
        assert p["cards_records"] == 38
        assert p["records_headline"] == 38, "the headline is records, not cards"

    def test_a_page_of_blank_slots_produces_no_headline_inflation(self):
        """The real tail page: 30 cards read, nothing extracted."""
        units = [Unit(1, "processing", 3, 12)]
        p = fuse_progress(progress_for(units),
                          scan(cards_done=90, cards_records=90, active_page=12),
                          page_from=3, page_to=12)
        assert p["records_headline"] == 90

        blank = fuse_progress(progress_for(units),
                              scan(cards_done=120, cards_records=90, active_page=12),
                              page_from=3, page_to=12)
        assert blank["cards_done"] == 120
        assert blank["records_headline"] == 90, (
            "30 blank lattice slots are work done, not voters found"
        )


class TestNoDivisionByZero:
    def test_a_zero_card_total_is_reported_as_unknown(self):
        units = [Unit(1, "processing", 3, 12)]
        p = fuse_progress(progress_for(units), scan(cards_total=0),
                          page_from=3, page_to=12)

        assert p["cards_live"] is True
        assert p["cards_known_total"] is None, "0 is not a denominator"


class TestMalformedPayloads:
    """The payload arrives over HTTP from another service. Never trust it."""

    @pytest.mark.parametrize("junk", [
        {}, {"cards_done": "many"}, {"cards_done": None},
        {"cards_done": 5, "idle_s": "soon"}, {"active_page": "seven"},
        {"cards_done": 5, "idle_s": None}, {"done": "yes"},
    ])
    def test_garbage_degrades_to_no_motion(self, junk):
        units = [Unit(1, "processing", 3, 12)]
        p = fuse_progress(progress_for(units), junk, page_from=3, page_to=12)

        assert p["cards_done"] == 0
        assert p["cards_records"] == 0
        assert p["cards_known_total"] is None
        assert isinstance(p["records_headline"], int)

    def test_a_string_active_page_matching_the_range_textually_is_rejected(self):
        """'4' is not 4. An int() coercion here would defeat the range test."""
        units = [Unit(1, "processing", 3, 12)]
        p = fuse_progress(progress_for(units), scan(active_page="4"),
                          page_from=3, page_to=12)

        assert p["cards_live"] is False

    def test_no_progress_at_all_still_renders(self):
        p = fuse_progress(None, None, None, None)

        assert p["state"] == "waiting"
        assert p["records_headline"] == 0
        assert p["cards_live"] is False


class TestTheCounterUrl:
    """A wrong URL here is invisible: the fetch 404s, the helper returns None,
    every page still renders, and the counter simply never moves. That is the
    original bug unchanged, so the derivation is pinned."""

    def test_progress_sits_beside_extract(self):
        from app.main import ocr_progress_snapshot  # noqa: F401 -- import check
        from app import config

        url = config.EXTRACTOR_URL.rsplit("/", 1)[0] + "/progress"
        assert url == "http://ocr:8082/ocr/progress", (
            f"derived {url}; the OCR service mounts routes under /ocr, so "
            "appending '/ocr/progress' would request /ocr/ocr/progress"
        )


class TestTheOriginalDictIsNotMutated:
    def test_fusing_leaves_live_progress_output_untouched(self):
        """live_progress() is shared with the status payload and other callers."""
        units = [Unit(1, "processing", 3, 12)]
        base = progress_for(units)
        before = dict(base)

        fuse_progress(base, scan(), page_from=3, page_to=12)

        assert base == before