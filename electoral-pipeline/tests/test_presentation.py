"""The admin UI is read by people who do not know what a "unit" is.

These lock in that machine values are always translated into something a
non-technical operator can act on, and that the helpers never raise on the
awkward values Postgres actually returns.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.presentation import (
    describe_error,
    document_progress,
    document_records_summary,
    duration_summary,
    event_label,
    file_size_summary,
    friendly_error,
    has_devanagari,
    progress_summary,
    records_summary,
    short_id,
    short_time,
    status_label,
    status_tone,
    time_ago,
)


class TestStatusWording:
    @pytest.mark.parametrize("raw,expected", [
        ("pending", "Waiting to start"),
        ("queued", "In the queue"),
        ("processing", "Being processed"),
        ("completed", "Finished"),
        ("retry", "Retrying"),
        ("failed", "Failed"),
        ("cancelled", "Stopped"),
        ("partial", "Partly finished"),
    ])
    def test_every_status_reads_as_english(self, raw, expected):
        assert status_label(raw) == expected
        assert raw not in status_label(raw) or raw == expected

    def test_unknown_status_degrades_to_readable_not_blank(self):
        """A status added later must still render, not show an empty badge."""
        assert status_label("some_new_status") == "Some New Status"
        assert status_label(None) == "Unknown"
        assert status_tone(None) == "muted"

    def test_no_internal_vocabulary_leaks_into_the_label(self):
        for raw in ("pending", "queued", "processing", "completed", "retry", "failed", "cancelled"):
            assert "_" not in status_label(raw)


class TestErrorWording:
    @pytest.mark.parametrize("code,headline", [
        ("EXTRACTION_TIMEOUT", "Took too long"),
        ("EXTRACTION_FAILED", "Could not read the PDF"),
        ("INTERNAL_ERROR", "Something went wrong inside the system"),
        ("EXTRACTOR_HTTP_ERROR", "The reading service had a problem"),
        ("INVALID_PDF", "The PDF could not be opened"),
    ])
    def test_known_codes_get_a_plain_headline(self, code, headline):
        assert describe_error(code, "raw technical text")[0] == headline

    def test_raw_message_never_becomes_the_headline(self):
        """"Unexpected error: Unknown OCR error" is what operators were shown.

        It names neither what broke nor what to do, so it must not be the
        headline even though it is available in the technical detail.
        """
        headline, _ = describe_error("INTERNAL_ERROR", "Unexpected error: Unknown OCR error")
        assert headline == "Something went wrong inside the system"
        assert "Unknown OCR error" not in headline

    def test_unknown_and_missing_codes_still_say_something(self):
        assert describe_error("TOTALLY_NEW_CODE", "x")[0]
        assert describe_error(None, None)[0]
        assert friendly_error(None, None)

    def test_advice_is_actionable(self):
        for code in ("EXTRACTION_TIMEOUT", "INTERNAL_ERROR", "EXTRACTION_FAILED"):
            _, advice = describe_error(code, "raw")
            assert advice, f"{code} has no advice"
            assert len(advice) > 20


class TestEventWording:
    def test_known_events_read_as_english(self):
        assert event_label("UNIT_FAILED") == "A batch of pages failed"
        assert event_label("SESSION_CREATED") == "Started work on the PDF"

    def test_unknown_event_degrades_readably(self):
        assert event_label("MYSTERY_THING") == "Mystery Thing"
        assert event_label(None) == "Unknown event"


class TestTimestamps:
    def test_naive_timestamps_do_not_raise(self):
        """Postgres returns naive datetimes for these columns.

        Comparing a naive value against an aware utcnow() raises TypeError,
        and a filter that raises inside a template yields a blank page -- the
        operator sees nothing at all instead of a time.
        """
        naive = datetime.utcnow() - timedelta(minutes=5)
        assert time_ago(naive) == "5 min ago"

    def test_aware_timestamps(self):
        assert time_ago(datetime.now(timezone.utc) - timedelta(hours=3)) == "3 hr ago"

    def test_future_timestamp_does_not_say_negative(self):
        assert time_ago(datetime.now(timezone.utc) + timedelta(minutes=5)) == "just now"

    def test_recent_and_missing(self):
        assert time_ago(datetime.now(timezone.utc)) == "just now"
        assert time_ago(None) is None

    def test_short_time_handles_both_kinds(self):
        assert short_time(datetime(2026, 9, 30, 14, 5)) == "30 Sep, 14:05"
        assert short_time(None) is None

    def test_string_timestamps_are_accepted(self):
        assert time_ago("2026-09-30T12:00:00")
        assert short_time("2026-09-30T12:00:00") == "30 Sep, 12:00"


class TestFormatting:
    def test_progress(self):
        assert progress_summary(0, 22) == "0 of 22 pages done"
        assert progress_summary(4, 22) == "4 of 22 pages done"
        assert progress_summary(22, 22) == "All 22 pages done"
        assert progress_summary(0, 0) == "Not started"
        assert progress_summary(None, None) == "Not started"

    def test_progress_does_not_say_1_pages(self):
        assert progress_summary(0, 1) == "0 of 1 page done"

    def test_records_pluralisation(self):
        assert records_summary(0) == "No voter records yet"
        assert records_summary(1) == "1 voter record saved"
        assert records_summary(2) == "2 voter records saved"
        assert records_summary(12500) == "12,500 voter records saved"

    def test_duration(self):
        assert duration_summary(45000) == "45 sec"
        assert duration_summary(125000) == "2 min 5 sec"
        assert duration_summary(3600000) == "1 hr"
        assert duration_summary(None) is None

    def test_duration_never_renders_zero_ms_for_missing(self):
        """The old UI printed "— ms" for a value that was never set."""
        assert duration_summary(None) is None

    def test_file_size_is_readable(self):
        assert file_size_summary(4781968) == "4.6 MB"
        assert file_size_summary(512) == "512 bytes"
        assert file_size_summary(None) is None

    def test_short_id_is_sayable(self):
        assert short_id("8dff1802-c516-437f-aa1a-49c43d0e494c") == "8dff1802…"
        assert short_id(None) == "—"


class TestDevanagariDetection:
    """Used by the UI to flag an English column that still holds Hindi."""

    def test_detects_hindi_in_an_english_column(self):
        assert has_devanagari("राजीव")
        assert has_devanagari("rajiva सक्सेना")

    def test_english_and_digits_pass(self):
        assert not has_devanagari("rajiva saksena")
        assert not has_devanagari("MALE")
        assert not has_devanagari(None)


class TestDocumentProgress:
    def test_no_session_reads_as_not_started(self):
        class Doc:  # minimal stand-in for a Document row
            pass
        assert document_progress(None) == "Not started"
        assert document_records_summary(None) == "No voter records yet"

    def test_completed_document_says_finished(self):
        class Session:
            status = "completed"
            pages_processed = 22
            pages_total = 22
            records_extracted = 660
        assert document_progress(Session) == "Finished"
        assert document_records_summary(Session) == "660 voter records saved"

    def test_partial_document_shows_where_it_stopped(self):
        class Session:
            status = "failed"
            pages_processed = 4
            pages_total = 22
            records_extracted = 120
        assert document_progress(Session) == "4 of 22 pages done"
        assert document_records_summary(Session) == "120 voter records saved"
