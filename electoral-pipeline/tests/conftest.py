"""Single place for import-path setup and per-test state isolation.

Three things this replaces:

1. Six near-identical `sys.path.insert(0, REPO_ROOT)` blocks spread across the
   test files. They only worked because pytest happened to run from the repo
   root; `pytest tests/test_contract.py` from anywhere else failed on
   `from app.extractor import ...`.
2. `tests/test_scripts/` holds ~25 diagnostic scripts, not tests. Several import
   the removed `pipeline.*` package and at least two call `sys.exit()` at module
   scope, which aborts collection for the whole directory with INTERNALERROR.
   `collect_ignore_glob` keeps them out of the default run without deleting
   work in progress -- they stay runnable by hand.
3. `config` is a module of import-time constants, so a test that monkeypatches
   an attribute leaks it into every later test in the session.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Do this before any test module is imported: test modules do `from app.x import
# y` at import time, and pytest imports them before running conftest fixtures.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_env_file() -> None:
    """Seed os.environ from .env for values the shell did not already provide.

    The app reads configuration from the process environment only. In Docker
    that works because every service carries `env_file: .env` in
    docker-compose.yml. Outside Docker -- `pytest`, or `uvicorn app.main:app` on
    a laptop -- nothing loads .env, so every variable falls back to its default
    in app/config.py.

    That was survivable while the defaults were all real working values. It is
    not now: ADMIN_TOKEN has no default on purpose, and app/config.py raises at
    import when it is missing. An unset ADMIN_TOKEN then aborts `import
    app.config`, which aborts collection of every test module -- the whole
    suite errors out before a single fixture runs, so this conftest's own
    state-restoration fixture never gets the chance to help.

    This reads .env without overriding anything already in the environment, so
    a real shell variable always wins. It deliberately does not parse quoted
    values, expand variables, or handle multiline entries -- it exists to make
    the test run see the same configuration the container sees, not to be a
    dotenv implementation.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


_load_env_file()


def pytest_ignore_collect(collection_path, config):
    """Keep tests/test_scripts/ out of collection.

    These are `__main__`-guarded diagnostic programs that read real PDFs and
    write CSV/PNG files. Several import `pipeline.pdf_extract`, a package that
    no longer exists, so collecting them raises ImportError at module scope --
    and two of them call sys.exit() during import, which pytest reports as an
    INTERNALERROR rather than as a collection error.

    They are excluded rather than moved because they are still useful by hand:
        python tests/test_scripts/trace_card_pipeline.py --help
    `tests/test_scripts/test_squash.py` and `test_whole_pdf_boundaries.py` are
    real tests and are re-included by pytest_collect_file below.
    """
    if "test_scripts" in collection_path.parts:
        return True
    return None


def pytest_collect_file(file_path, parent):
    """Re-admit the one real test that lives inside test_scripts/.

    pytest_ignore_collect drops the whole directory, so this has to be returned
    explicitly. `test_squash.py` is a genuine unit test of the transliteration
    squash helper and needs nothing but the repo root.

    `test_whole_pdf_boundaries.py` is deliberately NOT re-admitted, even though
    it is named like a test. It is a manual diagnostic: it imports the whole
    ocr_pdf_api service (which pulls in PaddleOCR, which pulls in torch) and
    writes one overlay PNG per page. On a host where torch's DLL load fails with
    Windows fatal exception 0xc0000139, that import aborts the interpreter --
    0xc0000139 is not a catchable Python exception, so ocr_pdf_api.py's
    try/except around `from paddleocr import PaddleOCR` never gets a chance to
    degrade to Tesseract-only. Collecting it made a bare `pytest` die before any
    test ran, which is the exact failure this conftest exists to prevent.

    It remains runnable by hand, which is how it is meant to be used:
        python tests/test_scripts/test_whole_pdf_boundaries.py [pdf]
    """
    if file_path.name == "test_squash.py":
        return pytest.Module.from_parent(parent, path=file_path)
    return None


@pytest.fixture(autouse=True)
def _restore_config():
    """Snapshot and restore every app.config attribute around each test.

    config is imported once and its constants are read directly
    (`config.ADMIN_TOKEN`, `config.HOST_MOUNT_STYLE`), so a test that sets an
    attribute to exercise a branch changes it for every test that runs after.
    Several existing tests already work around this by reloading the module,
    which is slow and leaks a second module object; restoring the attributes is
    both cheaper and complete.
    """
    from app import config

    saved = {k: v for k, v in vars(config).items() if k.isupper()}
    yield
    for key, value in saved.items():
        setattr(config, key, value)
    for key in [k for k in vars(config) if k.isupper() and k not in saved]:
        delattr(config, key)