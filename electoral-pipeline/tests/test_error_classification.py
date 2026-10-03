"""An ExtractionError must keep its code and its retry decision.

`extract()` had this handler chain:

    except FileNotFoundError as exc:      ... INVALID_PDF, retryable=False
    except httpx.TimeoutException as exc: ... EXTRACTION_TIMEOUT
    except (httpx.HTTPError, ValueError) as exc: ... EXTRACTION_SERVICE_UNAVAILABLE
    except Exception as exc:              ... INTERNAL_ERROR, retryable=False

`ExtractionError` is a plain `Exception` subclass carrying `.code` and
`.retryable`. Nothing caught it first, so the `except Exception` at the bottom
reached every deliberate raise in the function and rewrote it as INTERNAL_ERROR
with retryable=False.

`process_unit_job` reads exactly that:

    code = exc.code if isinstance(exc, ExtractionError) else ...
    fail_unit(..., code, ..., getattr(exc, "retryable", True))

So a reader that timed out -- transient, should retry on the next dispatch --
was recorded as a permanent internal error and the unit failed for good. The
specific code, chosen deliberately at each raise site, never reached the
operator who needed it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.extractor import ExtractionError  # noqa: E402


def _any_readable_file() -> str:
    """A real path extract() can open, in whatever checkout this is.

    The compose image carries `app/` but not pyproject.toml, so a test that
    hardcodes it passes locally and fails in the container. Any readable file in
    the repo serves: extract() only opens the handle and never parses it.
    """
    for candidate in ("pyproject.toml", "ocr_pdf_api.py", "requirements.txt"):
        path = REPO_ROOT / candidate
        if path.exists():
            return str(path)
    pytest.skip("no readable file in the repo root to stand in for a PDF")


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _run(monkeypatch, payload):
    """Drive the real extract() with a stubbed reader."""
    import app.extractor as extractor

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        return _StubResponse(payload)

    monkeypatch.setattr(extractor.httpx, "post", fake_post)
    return lambda: extractor.extract(
        document_id="00000000-0000-0000-0000-000000000000",
        document_location=_any_readable_file(),
        page_from=1,
        page_to=22,
    )


def test_reader_error_keeps_its_code(monkeypatch):
    """The regression: EXTRACTION_FAILED became INTERNAL_ERROR."""
    run = _run(monkeypatch, {"ok": False, "error": "page 7 is not a voter page"})
    with pytest.raises(ExtractionError) as exc:
        run()
    assert exc.value.code == "EXTRACTION_FAILED", (
        f"code was rewritten to {exc.value.code!r} by the generic handler"
    )


def test_reader_error_keeps_its_retryable_flag(monkeypatch):
    """EXTRACTION_FAILED is raised retryable=False; INTERNAL_ERROR also is.

    More importantly, the reverse case must hold too -- a timeout is retryable
    and must not be marked permanent.
    """
    run = _run(monkeypatch, {"ok": False, "error": "boom"})
    with pytest.raises(ExtractionError) as exc:
        run()
    assert exc.value.retryable is False


def test_timeout_stays_retryable(monkeypatch):
    """A reader timeout is transient. Marking it permanent strands the unit."""
    import app.extractor as extractor

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        raise extractor.httpx.TimeoutException("read timed out")

    monkeypatch.setattr(extractor.httpx, "post", fake_post)

    with pytest.raises(ExtractionError) as exc:
        extractor.extract(
            document_id="00000000-0000-0000-0000-000000000000",
            document_location=_any_readable_file(),
            page_from=1,
            page_to=22,
        )
    assert exc.value.code == "EXTRACTION_TIMEOUT", (
        f"a timeout was reported as {exc.value.code!r}"
    )
    assert exc.value.retryable is True, "a timeout must remain retryable"


def test_missing_file_is_not_swallowed_into_internal_error(tmp_path):
    """INVALID_PDF names the actual problem; INTERNAL_ERROR hides it."""
    from app.extractor import extract

    with pytest.raises(ExtractionError) as exc:
        extract(
            document_id="00000000-0000-0000-0000-000000000000",
            document_location=str(tmp_path / "absent.pdf"),
            page_from=1,
            page_to=22,
        )
    assert exc.value.code == "INVALID_PDF"
    assert exc.value.retryable is False


def test_unexpected_error_still_becomes_internal_error(monkeypatch):
    """Guard against over-correcting: the catch-all must still catch."""
    import app.extractor as extractor

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        raise RuntimeError("something genuinely unexpected")

    monkeypatch.setattr(extractor.httpx, "post", fake_post)

    with pytest.raises(ExtractionError) as exc:
        extractor.extract(
            document_id="00000000-0000-0000-0000-000000000000",
            document_location=_any_readable_file(),
            page_from=1,
            page_to=22,
        )
    assert exc.value.code == "INTERNAL_ERROR"
    assert exc.value.retryable is False


def test_extraction_error_handler_is_declared_first():
    """Source-level pin, so a future reordering fails here and not in production."""
    source = (REPO_ROOT / "app" / "extractor.py").read_text(encoding="utf-8")
    body = source[source.index("except ExtractionError:") :]
    positions = [
        body.index("except ExtractionError:"),
        body.index("except FileNotFoundError"),
        body.index("except httpx.TimeoutException"),
        body.index("except (httpx.HTTPError, ValueError)"),
        body.index("except Exception as exc:"),
    ]
    assert positions == sorted(positions), (
        "ExtractionError must be caught before any handler that would match it: "
        f"handler order is {positions}"
    )


def test_process_unit_reads_code_and_retryable_from_it():
    """The consumer side: confirm what fail_unit is actually handed."""
    import inspect

    from app import workflow

    source = inspect.getsource(workflow.process_unit_job)
    assert "exc.code if isinstance(exc, ExtractionError)" in source, (
        "process_unit_job must preserve the specific code"
    )
    assert 'getattr(exc, "retryable", True)' in source, (
        "the retry decision must come from the error, not a default"
    )