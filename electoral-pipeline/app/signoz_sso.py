"""Sign the current operator into SigNoz and hand the browser a one-time URL.

SigNoz keeps its own accounts. This asks SigNoz for a session token with the
deployment login, then sends the browser to a page SigNoz itself hosts. That
page stores the token and opens the SigNoz home screen, so the person does
not type a second password.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from app import config


class SignozError(RuntimeError):
    pass


def _post(path: str, payload: dict) -> httpx.Response:
    return httpx.post(
        config.SIGNOZ_INTERNAL_URL.rstrip("/") + path,
        json=payload,
        timeout=20,
    )


def login_tokens() -> tuple[str, str]:
    email = config.SIGNOZ_ADMIN_EMAIL
    password = config.SIGNOZ_ADMIN_PASSWORD
    if not email or not password:
        raise SignozError("SigNoz login is not configured.")

    response = _post("/api/v1/login", {"email": email, "password": password})
    if response.status_code == 401 or response.status_code == 404:
        created = _post("/api/v1/register", {
            "name": "Electoral admin",
            "orgName": config.SIGNOZ_ORG_NAME,
            "email": email,
            "password": password,
        })
        if created.status_code >= 400 and created.status_code != 409:
            raise SignozError(f"SigNoz setup failed ({created.status_code}).")
        response = _post("/api/v1/login", {"email": email, "password": password})
    if response.status_code >= 400:
        raise SignozError(f"SigNoz login failed ({response.status_code}).")

    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = body if isinstance(body, dict) else {}
    access = data.get("accessJwt") or data.get("accessToken") or ""
    refresh = data.get("refreshJwt") or data.get("refreshToken") or ""
    if not access:
        raise SignozError("SigNoz did not return a session token.")
    return access, refresh


def boot_url(access: str, refresh: str) -> str:
    public = config.SIGNOZ_PUBLIC_URL.rstrip("/")
    fragment = "access=" + quote(access) + "&refresh=" + quote(refresh) + "&email=" + quote(config.SIGNOZ_ADMIN_EMAIL)
    return f"{public}/boot.html#{fragment}"
