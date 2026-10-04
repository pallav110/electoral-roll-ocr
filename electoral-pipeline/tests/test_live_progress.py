"""Does the live counter report something true, and never something louder?

The counter exists because session.pages_processed and
session.records_extracted are written only by finalize(), so a page bound to
them shows 0 for the whole run and then jumps to the total. The replacement
counts from per-unit rows, which does move -- but only by projecting, and a
projection is a claim about work that has not happened yet.

So the tests here are mostly about refusals:

  * no rate yet            -> no estimate, rather than a made-up 0
  * a batch that is due    -> never shown as 100% done
  * a derived number       -> never labelled "saved"

The third one is the failure that would actually hurt. An operator watching a
counter climb and then finding 40 records in the database would have been told
something false with total confidence, which is the exact shape of bug this
repo's own comments keep warning about.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.presentation import eta_summary, live_progress


class FakeUnit:
    """Just the fields live_progress() reads."""

    def __init__(self, number, status, page_from, page_to,
                 records=0, started=None, completed=None):
        self.unit_number = number
        self.status = status
        self.page_from = page_from
        self.page_to = page_to
        self.records_extracted = records
        self.started_at = started
        self.completed_at = completed


BASE = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


def finished(number, page_from, page_to, records, duration, end_at):
    """A batch that finished `duration` seconds after it started."""
    end = BASE + timedelta(seconds=end_at)
    return FakeUnit(number, "completed", page_from, page_to, records,
                    started=end - timedelta(seconds=duration), completed=end)


class TestTheSettledNumbers:
    def test_only_finished_batches_count_as_settled(self):
        units = [
            finished(1, 1, 10, 90, 68, 68),
            FakeUnit(2, "processing", 11, 20),
        ]
        p = live_progress(units, at=at(100))
        assert p["pages_settled"] == 10
        assert p["records_settled"] == 90
        assert p["state"] == "working"

    def test_a_failed_batch_does_not_count_as_progress(self):
        """It produced nothing. Counting it would overstate the work done."""
        units = [
            finished(1, 1, 10, 90, 68, 68),
            FakeUnit(2, "failed", 11, 20, records=0,
                     started=at(70), completed=at(80)),
        ]
        p = live_progress(units, at=at(100))
        assert p["pages_settled"] == 10
        assert p["records_settled"] == 90

    def test_a_retrying_batch_does_not_count_as_progress(self):
        """It will run again, so its pages are not settled."""
        units = [
            finished(1, 1, 10, 90, 68, 68),
            FakeUnit(2, "retry", 11, 20, records=40,
                     started=at(70), completed=at(80)),
        ]
        p = live_progress(units, at=at(100))
        assert p["pages_settled"] == 10

    def test_settled_records_match_the_real_database_totals(self):
        """The six finished units in the live roll."""
        units = [
            finished(1, 1, 10, 74, 68, 68),
            finished(2, 11, 20, 90, 70, 140),
            finished(3, 21, 22, 7, 72, 212),
            finished(4, 1, 10, 90, 25, 240),
            finished(5, 11, 20, 90, 71, 311),
            finished(6, 21, 22, 90, 68, 379),
        ]
        p = live_progress(units, at=at(380))
        assert p["records_settled"] == 441
        assert p["pages_settled"] == 44
        assert p["state"] == "done"


class TestNoRateNoEstimate:
    def test_the_first_running_batch_gets_no_estimate(self):
        """Nothing has finished, so there is no rate to project from.

        The failure this prevents is the important one: showing 0 next to a
        counter implies "nothing has been read", when in fact a whole batch
        may be half done. The caller gets can_estimate=False and says so.
        """
        units = [FakeUnit(1, "processing", 1, 10, started=at(30))]
        p = live_progress(units, pages_total=22, at=at(60))
        assert p["can_estimate"] is False
        assert p["records_estimate"] is None
        assert p["pages_estimate"] == 0
        assert p["state"] == "working"

    def test_a_zero_duration_batch_produces_no_rate(self):
        """started_at == completed_at says nothing about how fast it runs."""
        units = [
            FakeUnit(1, "completed", 1, 10, records=90,
                     started=at(0), completed=at(0)),
            FakeUnit(2, "processing", 11, 20, started=at(10)),
        ]
        p = live_progress(units, pages_total=22, at=at(40))
        assert p["seconds_per_page"] is None
        assert p["can_estimate"] is False

    def test_a_batch_with_a_negative_span_is_not_used_as_a_rate(self):
        """Clock skew must not produce a negative rate."""
        units = [
            FakeUnit(1, "completed", 1, 10, records=90,
                     started=at(60), completed=at(30)),
            FakeUnit(2, "processing", 11, 20, started=at(70)),
        ]
        p = live_progress(units, pages_total=22, at=at(100))
        assert p["seconds_per_page"] is None

    def test_an_unknown_page_total_still_reports_settled_work(self):
        units = [finished(1, 1, 10, 90, 68, 68)]
        p = live_progress(units, at=at(100))
        assert p["pages_total"] == 10
        assert p["records_settled"] == 90


class TestTheEstimate:
    def test_half_a_batch_projects_to_about_half_the_records(self):
        """68 sec for 10 pages is 6.8 sec/page; 34 sec in is half a batch.

        90 records over 10 pages is 9 per page, so half of that batch is
        about 45 records -- not 90, and not 22. The projection is linear in
        pages, so the record figure follows from the page figure.
        """
        units = [
            finished(1, 1, 10, 90, 68, 68),
            FakeUnit(2, "processing", 11, 20, started=at(68)),
        ]
        p = live_progress(units, pages_total=22, at=at(102))
        assert p["seconds_per_page"] == pytest.approx(6.8)
        assert p["can_estimate"] is True
        assert p["records_per_page"] == pytest.approx(9.0)
        # The headline is the running total, so it includes the 90 already
        # saved: ~5 pages of the running batch x 9 per page = ~45 more.
        assert p["records_estimate"] == pytest.approx(135, abs=6)
        assert 14 <= p["pages_estimate"] <= 16

    def test_a_batch_past_its_expected_time_is_capped_short_of_full(self):
        """100% means finished. A batch that is merely due is not finished.

        Letting the bar reach full before the row flips would overstate work
        done -- and this function's entire job is to not do that.
        """
        units = [
            finished(1, 1, 10, 90, 68, 68),
            FakeUnit(2, "processing", 11, 20, started=at(68)),
        ]
        p = live_progress(units, pages_total=22, at=at(1000))   # 932s on a 68s batch
        assert p["can_estimate"] is True
        assert p["pages_estimate"] < 20
        assert p["pages_estimate"] == pytest.approx(19.5)
        assert p["records_estimate"] <= 179     # never more than all 22 pages

    def test_the_estimate_is_never_below_what_is_already_saved(self):
        units = [
            finished(1, 1, 10, 90, 68, 68),
            FakeUnit(2, "processing", 11, 20, started=at(68)),
        ]
        for offset in range(0, 300, 7):
            p = live_progress(units, pages_total=22, at=at(68 + offset))
            assert p["pages_estimate"] >= p["pages_settled"], offset
            if p["records_estimate"] is not None:
                assert p["records_estimate"] >= p["records_settled"], offset

    def test_the_rate_is_averaged_across_finished_batches(self):
        units = [
            finished(1, 1, 10, 90, 60, 60),
            finished(2, 11, 20, 90, 80, 140),
            FakeUnit(3, "processing", 21, 30, started=at(140)),
        ]
        p = live_progress(units, pages_total=30, at=at(140))
        assert p["seconds_per_page"] == pytest.approx((60 + 80) / 20)

    def test_a_rate_is_not_learned_from_a_failed_batch(self):
        units = [
            finished(1, 1, 10, 90, 60, 60),
            FakeUnit(2, "failed", 11, 20, records=0,
                     started=at(61), completed=at(61)),
            FakeUnit(3, "processing", 21, 30, started=at(140)),
        ]
        p = live_progress(units, pages_total=30, at=at(140))
        assert p["seconds_per_page"] == pytest.approx(6.0)

    def test_two_finished_batches_do_not_double_the_record_rate(self):
        """A real bug this caught, pinned so it cannot come back.

        Summing per-batch ratios instead of dividing totals: two batches of
        90 records over 10 pages each give 9 + 9 = 18 records per page, and
        every projection after the second batch would be double what it
        should be. Correct answer is 180 records / 20 pages = 9.
        """
        units = [
            finished(1, 1, 10, 90, 68, 68),
            finished(2, 11, 20, 90, 68, 136),
            FakeUnit(3, "processing", 21, 30, started=at(136)),
        ]
        p = live_progress(units, pages_total=30, at=at(136))
        assert p["records_per_page"] == pytest.approx(9.0)
        assert p["records_settled"] == 180

    def test_unequal_batch_sizes_are_weighted_not_averaged(self):
        """A 2-page batch and a 20-page batch do not count equally."""
        units = [
            finished(1, 1, 2, 18, 14, 14),        # 9 per page
            finished(2, 3, 22, 180, 136, 150),    # 9 per page
            FakeUnit(3, "processing", 23, 32, started=at(150)),
        ]
        p = live_progress(units, pages_total=32, at=at(150))
        assert p["records_per_page"] == pytest.approx(9.0)
        # Timed pages are 2 + 20 = 22, seconds are 14 + 136 = 150. The payload
        # rounds to 2dp for readability, so compare against the rounded form.
        assert p["seconds_per_page"] == round(150 / 22, 2)

    def test_a_batch_that_produced_no_records_is_skipped_for_the_record_rate(self):
        """A zero-record batch has no records-per-page to contribute.

        Including it in the denominator would divide real records by pages
        that produced none, understating the rate.
        """
        units = [
            finished(1, 1, 10, 0, 60, 60),        # a page that read as blank
            finished(2, 11, 20, 90, 60, 120),
            FakeUnit(3, "processing", 21, 30, started=at(120)),
        ]
        p = live_progress(units, pages_total=30, at=at(120))
        assert p["records_per_page"] == pytest.approx(9.0)
        # ...but it still counts toward the time rate, because it took time.
        assert p["seconds_per_page"] == pytest.approx(6.0)


class TestStates:
    def test_a_finished_session_is_done(self):
        units = [finished(1, 1, 10, 90, 68, 68),
                 finished(2, 11, 20, 7, 60, 128)]
        assert live_progress(units, at=at(200))["state"] == "done"

    def test_a_finished_session_reports_no_eta(self):
        """An ETA on completed work is noise."""
        units = [finished(1, 1, 10, 90, 68, 68)]
        assert live_progress(units, pages_total=10, at=at(200))["eta_seconds"] == 0

    def test_between_batches_is_not_the_same_as_waiting(self):
        units = [finished(1, 1, 10, 90, 68, 68),
                 FakeUnit(2, "queued", 11, 20)]
        p = live_progress(units, pages_total=20, at=at(70))
        assert p["state"] == "between"
        assert p["running_unit_number"] is None

    def test_nothing_started_yet_reads_as_waiting(self):
        p = live_progress([FakeUnit(1, "pending", 1, 10),
                           FakeUnit(2, "pending", 11, 20)],
                          pages_total=20, at=at(0))
        assert p["state"] == "waiting"
        assert p["records_settled"] == 0

    def test_no_units_at_all_does_not_raise(self):
        p = live_progress([], at=at(0))
        assert p["state"] == "waiting"
        assert p["records_settled"] == 0

    def test_a_none_session_does_not_raise(self):
        assert live_progress(None, at=at(0))["state"] == "waiting"

    def test_the_running_batch_is_named(self):
        units = [finished(1, 1, 10, 90, 68, 68),
                 FakeUnit(2, "processing", 11, 20, started=at(68))]
        p = live_progress(units, pages_total=22, at=at(90))
        assert p["running_unit_number"] == 2
        assert p["running_pages"] == 10

    def test_timestamps_may_arrive_as_strings(self):
        """The JSON status endpoint sends isoformat strings, so the browser
        path feeds these back in as text. A datetime.fromisoformat failure must
        degrade, not raise -- a raise inside the render is a blank page."""
        units = [
            FakeUnit(1, "completed", 1, 10, records=90,
                     started="2026-10-04T12:00:00+00:00",
                     completed="2026-10-04T12:01:08+00:00"),
            FakeUnit(2, "processing", 11, 20,
                     started="2026-10-04T12:01:08+00:00"),
        ]
        p = live_progress(units, pages_total=22,
                          at=datetime(2026, 10, 4, 12, 1, 42, tzinfo=timezone.utc))
        assert p["seconds_per_page"] == pytest.approx(6.8)
        assert p["can_estimate"] is True

    def test_unparseable_timestamps_degrade_to_no_rate(self):
        units = [
            FakeUnit(1, "completed", 1, 10, records=90,
                     started="not-a-date", completed="also-not"),
            FakeUnit(2, "processing", 11, 20, started="nope"),
        ]
        p = live_progress(units, pages_total=22, at=at(100))
        assert p["seconds_per_page"] is None
        assert p["can_estimate"] is False

    def test_naive_timestamps_are_accepted(self):
        """Postgres returns naive datetimes for these columns on some paths."""
        units = [
            FakeUnit(1, "completed", 1, 10, records=90,
                     started=datetime(2026, 10, 4, 12, 0, 0),
                     completed=datetime(2026, 10, 4, 12, 1, 8)),
            FakeUnit(2, "processing", 11, 20,
                     started=datetime(2026, 10, 4, 12, 1, 8)),
        ]
        p = live_progress(units, pages_total=22,
                          at=datetime(2026, 10, 4, 12, 1, 42))
        assert p["can_estimate"] is True


class TestEta:
    def test_no_eta_before_there_is_a_rate(self):
        units = [FakeUnit(1, "processing", 1, 10, started=at(30))]
        assert live_progress(units, pages_total=22, at=at(60))["eta_seconds"] is None

    def test_an_eta_is_produced_once_a_rate_exists(self):
        units = [finished(1, 1, 10, 90, 68, 68),
                 FakeUnit(2, "processing", 11, 20, started=at(68))]
        p = live_progress(units, pages_total=22, at=at(102))
        # At t=102s the running batch has done 34/68 of its work, so ~5 of
        # its 10 pages are read: 22 - 15 = 7 pages left, at 6.8 s/page.
        assert p["eta_seconds"] == pytest.approx(48, abs=4)

    def test_eta_summary_is_hedged(self):
        """A projection from one batch is not a promise."""
        assert eta_summary(30) == "less than a minute left"
        assert eta_summary(240) == "about 4 min left"
        assert eta_summary(3600 * 2) == "about 2 hr left"
        assert eta_summary(3600 * 2 + 180) == "about 2 hr 3 min left"

    def test_eta_summary_says_nothing_when_unknown(self):
        assert eta_summary(None) is None
        assert eta_summary(-5) is None