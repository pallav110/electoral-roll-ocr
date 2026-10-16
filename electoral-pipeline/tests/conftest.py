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
import re
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


def _point_tests_at_a_separate_database() -> None:
    """Redirect app.db at a test database before any test module imports it.

    Tests here create real rows, and until now they created them in the same
    database the running application reads. That is not a theoretical problem:
    the beat dispatches every ten seconds against the same rows, so a test's
    fixture documents were picked up by a live dispatch tick mid-test, and rows
    left behind by a failing test were dispatched as though they were real
    uploads. Forty-seven such documents accumulated in the live table.

    The window tests are the sharpest example. They stub the in-flight count so
    they can say "5 pending units, a window of 2, therefore 2 are published".
    Against the live table the beat published their units first, the reclaim
    then took the remainder, and the test asserted on someone else's work.

    Everything here is local, so the rule is simple: a test run may never write
    to the database the application serves from. Overriding DATABASE_URL here --
    before app.db is imported anywhere -- is what makes that true, and it is why
    this has to happen at conftest import time rather than in a fixture.

    Falls back to a name derived from the app's own URL when TEST_DATABASE_URL
    is unset, so a missing setting isolates the run rather than silently
    pointing it at the live database.
    """
    app_url = os.environ.get("DATABASE_URL")
    if not app_url:
        return  # config.py has its own default; nothing to redirect

    test_url = os.environ.get("TEST_DATABASE_URL") or re.sub(
        r"/([^/?]+)(\?|$)", r"/electoral_test\2", app_url
    )
    os.environ["DATABASE_URL"] = test_url

    # Redis db 1 for tests: the same broker, but queues the running workers are
    # not subscribed to, so a test publishing a fixture unit cannot start OCR on
    # it. Sharing db 0 would hand test work to the live pipeline.
    redis_url = os.environ.get("REDIS_URL", "")
    base, _, tail = redis_url.rpartition("/")
    if base and tail.isdigit():
        os.environ["REDIS_URL"] = f"{base}/1"


_point_tests_at_a_separate_database()


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


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create the schema once per session, and refuse to run without it.

    The database is not created by the compose `init` service -- that runs
    against the application's own database, before a test process exists -- so a
    test run against a fresh checkout finds an empty `electoral_test` and every
    table-missing error at once. Creating it here, from the models, keeps the
    test schema in step with the code by construction.

    Refuses rather than continuing when the database cannot be reached. Silently
    skipping would be worse than failing: the queue tests assert on real rows,
    and a run that quietly tested nothing looks exactly like a run that passed.
    """
    from sqlalchemy import create_engine, text

    from app import config
    from app.db import Base, engine

    # Import the models for their side effect of registering on Base. Without
    # this, create_all finds an empty registry and creates nothing -- which
    # looks like a working database right up until the first INSERT.
    import app.models  # noqa: F401

    name = config.DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    try:
        # CREATE DATABASE cannot run from inside a transaction, and connecting to
        # the target database is impossible before it exists -- so this connects
        # to the maintenance database instead. That is what `postgres` is for.
        maintenance = re.sub(r"/[^/?]+(\?|$)", r"/postgres\1", config.DATABASE_URL)
        admin = create_engine(maintenance, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
        admin.dispose()
    except Exception as exc:  # pragma: no cover - depends on local Postgres
        pytest.exit(
            f"Cannot prepare the test database ({config.DATABASE_URL!r}): {exc}\n"
            "Tests refuse to run without it rather than passing against nothing. "
            "Point TEST_DATABASE_URL at a reachable database and try again.",
            returncode=1,
        )

    Base.metadata.create_all(engine)

    # Empty it before the first test runs. A test that fails mid-body leaves rows
    # behind, and the next run would start on top of them -- which is how a
    # dispatch test ends up asserting on a row it did not create.
    #
    # This empties every table, so it is guarded by a check on the database it is
    # about to connect to rather than on the redirect above having worked. That
    # redirect is best-effort: it returns early when DATABASE_URL is unset, and
    # its fallback rewrites the name in whatever URL it was given. Between them,
    # "tests point somewhere safe" is an assumption; reading the name back off
    # the live connection is a fact. The suffix is required, and only a name
    # ending in it -- or the exact name TEST_DATABASE_URL asked for -- gets past.
    # Anything else stops the run rather than emptying the application's data.
    _assert_it_is_the_test_database(engine, name)
    with engine.begin() as conn:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    yield


def _assert_it_is_the_test_database(engine, expected: str) -> None:
    """Refuse to run unless the connected database is unambiguously a test one."""
    from sqlalchemy import text

    with engine.connect() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar()
    configured = os.environ.get("TEST_DATABASE_URL", "")
    allowed = {expected}
    if configured:
        allowed.add(configured.rsplit("/", 1)[-1].split("?")[0])

    if actual not in allowed or not actual.endswith("_test"):
        pytest.exit(
            f"Refusing to run: the test engine is connected to {actual!r}, which "
            f"is not a test database. Emptied tables would destroy real data. "
            f"Point TEST_DATABASE_URL at a database whose name ends in '_test'.",
            returncode=1,
        )


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