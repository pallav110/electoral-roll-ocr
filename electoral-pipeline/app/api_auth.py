"""Bearer tokens for the HTTP API.

The browser keeps using the session cookie. A program sends
``Authorization: Bearer <token>`` instead. Two kinds of token are accepted:

* ``ADMIN_TOKEN`` from the environment. This is the deployment credential. It
  acts as the bootstrap superadmin and is how a backend creates the first
  extra accounts without clicking through the website.
* A personal token issued when an account is created. It belongs to that
  one user and can be revoked by issuing another one.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import config
from app.models import AdminUser


def ensure_token_table(db: Session) -> None:
    db.execute(text(
        """
        CREATE TABLE IF NOT EXISTS admin_api_tokens (
            user_id UUID PRIMARY KEY REFERENCES admin_users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    ))
    db.commit()


def _equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def issue_api_token(db: Session, user: AdminUser) -> str:
    """Replace this user's API token and return the new secret once."""
    ensure_token_table(db)
    secret = secrets.token_urlsafe(32)
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    db.execute(
        text(
            """
            INSERT INTO admin_api_tokens (user_id, token_hash)
            VALUES (:user_id, :token_hash)
            ON CONFLICT (user_id) DO UPDATE
            SET token_hash = EXCLUDED.token_hash, created_at = now()
            """
        ),
        {"user_id": user.id, "token_hash": digest},
    )
    db.commit()
    return secret


def user_from_bearer(db: Session, token: str) -> AdminUser | None:
    token = token.strip()
    if not token:
        return None
    if _equal(token, config.ADMIN_TOKEN):
        return db.scalar(
            select(AdminUser).where(AdminUser.username == config.BOOTSTRAP_ADMIN_USERNAME)
        )
    ensure_token_table(db)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    user_id = db.execute(
        text("SELECT user_id FROM admin_api_tokens WHERE token_hash = :digest"),
        {"digest": digest},
    ).scalar()
    if user_id is None:
        return None
    user = db.get(AdminUser, user_id)
    if user is None or not user.is_active:
        return None
    return user
