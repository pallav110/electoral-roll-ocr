"""Tests for the Patch A progress counter, without touching ocr_pdf_api.py.

The counter is defined here verbatim -- copied from PATCH_A's proposed source,
not reimplemented -- so these tests exercise the exact code that would be
applied. The alternative, testing a near-copy, would let the real patch drift
from what was verified, which is the failure mode the golden-diff discipline
in this repo exists to prevent.

Nothing here imports ocr_pdf_api. That file is bind-mounted read-only into the
ocr container and carries uncommitted work; importing it would drag in
PyMuPDF, tesseract and paddle at module load for a test that needs none of
them, and would make the suite fail on a machine without those binaries.

Run against the real session in the database: 7 units over pages 3-22
(20 pages), 600 grid positions, 531 records, ~70s per unit.
"""
import threading
import time
from typing import Any

import pytest

# ── The proposed code, verbatim ────────────────────────────────────────────
_PROGRESS_LOCK = threading.Lock()
_PROGRESS_SNAPSHOT: dict[str, Any] = {
    "cards_done": 0,
    "pages_done": 0,
    "cards_total": 0,
    "pages_total": 0,
    "cards_records": 0,
    "started_at": 0.0,
    "updated_at": 0.0,
    "active_page": None,
    "done": False,
}


def _progress_reset(pages_total: int = 0) -> None:
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT.update({
            "cards_done": 0,
            "pages_done": 0,
            "cards_total": 0,
            "pages_total": int(pages_total or 0),
            "cards_records": 0,
            "started_at": now_ts,
            "updated_at": now_ts,
            "active_page": None,
            "done": False,
        })


def _progress_card_done(*, page_number: int, records: int) -> None:
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["cards_done"] += 1
        _PROGRESS_SNAPSHOT["cards_records"] += max(0, int(records or 0))
        _PROGRESS_SNAPSHOT["active_page"] = page_number
        _PROGRESS_SNAPSHOT["updated_at"] = now_ts


def _progress_page_done(page_number: int, cards_on_page: int) -> None:
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["pages_done"] += 1
        _PROGRESS_SNAPSHOT["cards_total"] += max(0, int(cards_on_page or 0))
        _PROGRESS_SNAPSHOT["active_page"] = page_number
        _PROGRESS_SNAPSHOT["updated_at"] = now_ts


def _progress_finish() -> None:
    now_ts = time.time()
    with _PROGRESS_LOCK:
        _PROGRESS_SNAPSHOT["done"] = True
        _PROGRESS_SNAPSHOT["updated_at"] = now_ts


def _progress_snapshot() -> dict[str, Any]:
    with _PROGRESS_LOCK:
        snap = dict(_PROGRESS_SNAPSHOT)
    snap["elapsed_s"] = round(
        max(0.0, snap["updated_at"] - snap["started_at"]), 1
    ) if snap["started_at"] else 0.0
    snap["idle_s"] = round(
        max(0.0, time.time() - snap["updated_at"]), 1
    ) if snap["updated_at"] else 0.0
    return snap
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean():
    _progress_reset(pages_total=20)   # this session: pages 3-22
    yield
    _progress_reset(pages_total=0)


class TestItCountsWhatItClaims:
    def test_a_fresh_reset_is_all_zero(self):
        s = _progress_snapshot()
        assert s["cards_done"] == 0
        assert s["pages_done"] == 0
        assert s["cards_records"] == 0
        assert s["done"] is False

    def test_cards_accumulate(self):
        for i in range(300):
            _progress_card_done(page_number=i // 30 + 1, records=1)
        assert _progress_snapshot()["cards_done"] == 300

    def test_a_blank_card_counts_as_a_card_but_not_a_record(self):
        """_voter_card_rects() returns 30 lattice positions per page whether or
        not the cell is filled; a tail page emits 14 and 7. So a position that
        yields no record is still work done, and must still move the counter --
        otherwise the last two pages of every roll look stalled."""
        _progress_card_done(page_number=19, records=0)
        _progress_card_done(page_number=19, records=0)
        _progress_card_done(page_number=19, records=1)
        s = _progress_snapshot()
        assert s["cards_done"] == 3
        assert s["cards_records"] == 1

    def test_pages_accumulate_and_track_their_card_count(self):
        _progress_page_done(1, 30)
        _progress_page_done(2, 30)
        s = _progress_snapshot()
        assert s["pages_done"] == 2
        assert s["cards_total"] == 60

    def test_the_tail_page_lattice_is_counted_not_the_records(self):
        """Page 19 emits 14 records from 30 positions; page 21 emits 7;
        page 22 emits none at all."""
        _progress_page_done(19, 30)
        _progress_page_done(21, 30)
        _progress_page_done(22, 30)
        s = _progress_snapshot()
        assert s["cards_total"] == 90
        assert s["pages_done"] == 3
        assert s["cards_records"] == 0

    def test_the_active_page_is_the_most_recent(self):
        _progress_card_done(page_number=4, records=1)
        _progress_card_done(page_number=5, records=1)
        assert _progress_snapshot()["active_page"] == 5


class TestTheRollsTotals:
    def test_the_real_531_record_roll_reaches_531(self):
        """The actual session in the database, not an idealised one.

        Read off extraction_units and electoral_records rather than invented:
          units    7 batches covering pages 3-22 -> 20 pages
          records  531
          grid     20 x 30 = 600 positions

        The gap between 600 positions and 531 records is the point of the
        two counters. _voter_card_rects() returns 30 slots per page whether
        or not a cell is filled, so the last pages are mostly blank lattice
        and page 22 yields nothing at all. Conflating the two would make the
        counter overshoot by 69.
        """
        # pages 3..18 full, 19 short, 20 full, 21 short, 22 empty
        records = ([30] * 16) + [14, 30, 7, 0]
        positions = [30] * 20
        assert len(records) == len(positions) == 20
        assert sum(positions) == 600
        assert sum(records) == 531

        for index, (slots, recs) in enumerate(zip(positions, records)):
            page = index + 3                      # this session starts at page 3
            for slot in range(slots):
                _progress_card_done(page_number=page,
                                    records=1 if slot < recs else 0)
            _progress_page_done(page, slots)

        s = _progress_snapshot()
        assert s["cards_done"] == 600
        assert s["cards_records"] == 531
        assert s["pages_done"] == 20
        _progress_finish()
        assert _progress_snapshot()["done"] is True

    def test_the_grid_overshoots_the_records_by_design(self):
        """600 slots, 531 records. The counter must not report 600 as records."""
        records = ([30] * 16) + [14, 30, 7, 0]
        positions = [30] * 20
        for index, (slots, recs) in enumerate(zip(positions, records)):
            page = index + 3
            for slot in range(slots):
                _progress_card_done(page_number=page,
                                    records=1 if slot < recs else 0)
            _progress_page_done(page, slots)
        s = _progress_snapshot()
        assert s["cards_done"] - s["cards_records"] == 69
        assert s["cards_records"] == 531

    def test_finish_does_not_change_the_counts(self):
        _progress_card_done(page_number=1, records=1)
        before = _progress_snapshot()
        _progress_finish()
        after = _progress_snapshot()
        assert after["cards_done"] == before["cards_done"]
        assert after["cards_records"] == before["cards_records"]
        assert after["done"] is True


class TestResetBetweenRuns:
    def test_a_second_run_starts_from_zero(self):
        """A stale count leaking into the next request would show a finished
        roll still ticking up, or worse, show 531 before any work started."""
        for i in range(300):
            _progress_card_done(page_number=1, records=1)
        _progress_finish()
        assert _progress_snapshot()["cards_done"] == 300

        _progress_reset(pages_total=20)
        s = _progress_snapshot()
        assert s["cards_done"] == 0
        assert s["cards_total"] == 0
        assert s["records" if "records" in s else "cards_records"] == 0
        assert s["done"] is False
        assert s["started_at"] > 0

    def test_the_page_total_is_carried_into_the_reset(self):
        _progress_reset(pages_total=20)
        assert _progress_snapshot()["pages_total"] == 20

    def test_a_reset_with_no_page_count_is_zero_not_a_crash(self):
        _progress_reset()
        assert _progress_snapshot()["pages_total"] == 0
        assert _progress_snapshot()["cards_done"] == 0


class TestItCannotBreakExtraction:
    def test_a_none_record_count_does_not_raise(self):
        """The call site passes a record that may be None on a blank lattice
        position. A TypeError there would abort the whole page."""
        _progress_card_done(page_number=1, records=None)
        _progress_card_done(page_number=1, records=0)
        assert _progress_snapshot()["cards_done"] == 2

    def test_a_negative_record_count_is_clamped(self):
        _progress_card_done(page_number=1, records=-5)
        assert _progress_snapshot()["cards_records"] == 0

    def test_a_none_page_count_does_not_raise(self):
        _progress_page_done(1, None)
        assert _progress_snapshot()["cards_total"] == 0

    def test_the_snapshot_is_a_copy_not_the_live_dict(self):
        """Handing out the live dict would let a serialiser mutate the
        counter's internals -- or a caller hold a reference across a reset."""
        snap = _progress_snapshot()
        snap["cards_done"] = 999999
        assert _progress_snapshot()["cards_done"] == 0


class TestConcurrency:
    """The card loop runs under ThreadPoolExecutor, so these run concurrently
    for real. A lost update would make the counter read low -- the exact
    symptom of "it only updates after the page is scanned"."""

    def test_eight_threads_lose_no_updates(self):
        def worker(n):
            for _ in range(200):
                _progress_card_done(page_number=n, records=1)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _progress_snapshot()["cards_done"] == 1600

    def test_counting_does_not_raise_under_contention(self):
        errors = []

        def worker():
            try:
                for _ in range(300):
                    _progress_card_done(page_number=1, records=1)
                    _progress_snapshot()
            except Exception as exc:      # noqa: BLE001 -- recording, not handling
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert _progress_snapshot()["cards_done"] == 2400

    def test_elapsed_never_goes_backwards(self):
        """The UI reads elapsed_s; a negative span would render "-3s"."""
        _progress_card_done(page_number=1, records=1)
        first = _progress_snapshot()["elapsed_s"]
        for _ in range(50):
            _progress_card_done(page_number=1, records=1)
        assert _progress_snapshot()["elapsed_s"] >= first >= 0


class TestTheEndpointShape:
    def test_every_field_is_json_serialisable(self):
        """The route returns this dict directly. A non-serialisable value here
        is a 500 on the progress endpoint -- which would look exactly like a
        broken counter, with nothing in the logs to explain it."""
        import json
        for i in range(10):
            _progress_card_done(page_number=1, records=1)
        _progress_page_done(1, 30)
        payload = json.dumps(_progress_snapshot())
        assert json.loads(payload)["cards_done"] == 10

    def test_idle_s_is_present_so_a_stall_is_visible(self):
        """Seconds since the last card. A frozen counter is indistinguishable
        from a slow one without it, and 'is it stuck?' is the first question
        an operator asks."""
        _progress_card_done(page_number=1, records=1)
        assert _progress_snapshot()["idle_s"] < 5

    def test_a_snapshot_before_any_work_is_serialisable(self):
        import json
        assert json.loads(json.dumps(_progress_snapshot()))["cards_done"] == 0