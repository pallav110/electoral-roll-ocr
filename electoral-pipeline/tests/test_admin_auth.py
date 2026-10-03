"""The admin credential must fail closed, and must not crash on non-ASCII.

Two distinct defects, both verified before being fixed:

1. `secrets.compare_digest` accepts only ASCII str. Given a token containing any
   non-ASCII character it raises TypeError instead of returning False. Because
   `admin` is a FastAPI dependency with no global exception handler, that
   TypeError escapes as a 500 on all 23 routes carrying Depends(admin) -- the
   operator sees a 500, concludes the service is broken, and never learns the
   credential is the problem. A password manager produces a token with an
   accented or non-Latin character without effort.

2. ADMIN_TOKEN defaulted to 'change-me-before-deployment', which .env.example
   also shipped. A deployment missing the variable authenticated every route
   against a password published in the repository.
"""
from __future__ import annotations

import importlib
import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PLACEHOLDER = "change-me-before-deployment"


# --------------------------------------------------------------------------
# compare_digest behaviour, pinned so the fix's premise cannot silently change
# --------------------------------------------------------------------------

def test_compare_digest_rejects_non_ascii_str():
    """Why the fix exists: str comparison raises rather than returning False."""
    with pytest.raises(TypeError):
        secrets.compare_digest("pässwörd", "pässwörd")


def test_encoding_both_operands_is_the_fix():
    assert secrets.compare_digest("pässwörd".encode(), "pässwörd".encode()) is True
    assert secrets.compare_digest("a".encode(), "b".encode()) is False


# --------------------------------------------------------------------------
# ADMIN_TOKEN fail-closed behaviour
# --------------------------------------------------------------------------


def _reload_config(monkeypatch, value: str | None):
    """Import app.config fresh with ADMIN_TOKEN set to `value` (or unset)."""
    if value is None:
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ADMIN_TOKEN", value)
    monkeypatch.delenv("ALLOW_INSECURE_DEFAULT_ADMIN", raising=False)
    import app.config

    return importlib.reload(app.config)


def test_missing_token_refuses_to_start(monkeypatch):
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN is not set"):
        _reload_config(monkeypatch, None)


def test_empty_token_refuses_to_start(monkeypatch):
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN is not set"):
        _reload_config(monkeypatch, "")


def test_published_placeholder_refuses_to_start(monkeypatch):
    """The exact string .env.example used to ship must not be accepted."""
    with pytest.raises(RuntimeError, match="published placeholder"):
        _reload_config(monkeypatch, PLACEHOLDER)


def test_real_token_loads(monkeypatch):
    config = _reload_config(monkeypatch, secrets.token_urlsafe(24))
    assert config.ADMIN_TOKEN
    assert config.ADMIN_TOKEN != PLACEHOLDER


def test_non_ascii_token_loads_and_is_comparable(monkeypatch):
    """A token with a non-ASCII character must load, not crash.

    This is the exact input that used to produce a 500 on every route.
    """
    token = "pässwörd-令牌-" + secrets.token_urlsafe(8)
    config = _reload_config(monkeypatch, token)
    assert config.ADMIN_TOKEN == token
    # And the comparison the auth dependency performs must be safe on it.
    assert (
        secrets.compare_digest(
            token.encode("utf-8"), config.ADMIN_TOKEN.encode("utf-8")
        )
        is True
    )


def test_escape_hatch_still_works_but_warns(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_DEFAULT_ADMIN", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    import app.config

    with pytest.warns(RuntimeWarning, match="published placeholder"):
        config = importlib.reload(app.config)
    assert config.ADMIN_TOKEN == PLACEHOLDER


def test_env_example_does_not_ship_a_placeholder():
    """The example file must not teach the configuration that now refuses to boot."""
    example = REPO_ROOT / ".env.example"
    if not example.exists():
        pytest.skip("no .env.example in this checkout")
    for line in example.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ADMIN_TOKEN="):
            value = line.split("=", 1)[1].strip()
            assert value != PLACEHOLDER, (
                ".env.example still contains the published placeholder"
            )
            assert value == "", (
                ".env.example must not contain a real credential"
            )


# --------------------------------------------------------------------------
# The auth dependency itself, exercised directly
# --------------------------------------------------------------------------


def test_admin_dependency_returns_401_not_500_for_non_ascii(monkeypatch):
    """The regression in one test: a non-ASCII credential is unauthorized.

    Before the fix this raised TypeError out of the dependency, which FastAPI
    surfaces as a 500.
    """
    from fastapi import HTTPException

    from app import config as config_module
    from app.main import admin

    token = "pässwörd-令牌"
    monkeypatch.setattr(config_module, "ADMIN_TOKEN", token)

    class Creds:
        username = "admin"
        password = token + "wrong"

    with pytest.raises(HTTPException) as exc:
        admin(Creds())
    assert exc.value.status_code == 401


def test_admin_dependency_accepts_correct_non_ascii_token(monkeypatch):
    from app import config as config_module
    from app.main import admin

    token = "pässwörd-令牌"
    monkeypatch.setattr(config_module, "ADMIN_TOKEN", token)

    class Creds:
        username = "admin"
        password = token

    assert admin(Creds()) is None  # no exception == authenticated


def test_admin_dependency_rejects_wrong_username(monkeypatch):
    from fastapi import HTTPException

    from app import config as config_module
    from app.main import admin

    monkeypatch.setattr(config_module, "ADMIN_TOKEN", "s3cret")

    class Creds:
        username = "root"
        password = "s3cret"

    with pytest.raises(HTTPException) as exc:
        admin(Creds())
    assert exc.value.status_code == 401