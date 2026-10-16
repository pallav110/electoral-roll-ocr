"""Serve the MinIO console under /minio on the web app's own origin.

The public hostname sits behind Cloudflare and a reverse proxy that forward
only the web port, so the console's own port (9001) is unreachable from a
browser and a link to it hangs until timeout. MinIO must be started with
MINIO_BROWSER_REDIRECT_URL ending in this prefix so the console emits asset
and API URLs under it; the prefix is stripped before forwarding.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from app import config


log = logging.getLogger(__name__)
PREFIX = "/minio"
# Deliberately outside PREFIX: the console's own client-side router owns
# every path under /minio, including /minio/login.
SSO_PATH = "/minio-sso"

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

router = APIRouter()
_client = httpx.AsyncClient(
    base_url=config.MINIO_CONSOLE_INTERNAL_URL,
    timeout=httpx.Timeout(None, connect=10.0),
    follow_redirects=False,
)


def _upstream_path(path: str, query: str) -> str:
    target = "/" + path
    return f"{target}?{query}" if query else target


def _signed_in(scope_session: dict) -> bool:
    return bool(scope_session.get("admin_user_id"))


def require_app_login(request: Request) -> None:
    """Only people signed in to the pipeline may reach the MinIO console.

    MinIO has its own login, but exposing it to the internet means anyone can
    sit on that screen and try passwords. The pipeline login already decides
    who is staff, so it is the gate here as well. The browser is sent to the
    pipeline sign-in and brought back to the console afterwards.
    """
    if _signed_in(request.session):
        return
    if request.headers.get("accept", "").startswith("text/html") or request.method == "GET":
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login?next=" + quote(SSO_PATH, safe="")},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in first.")


def _console_cookie_attrs(request: Request) -> dict:
    forwarded = request.headers.get("x-forwarded-proto", "")
    secure = request.url.scheme == "https" or forwarded.startswith("https")
    return {"path": "/", "httponly": True, "samesite": "lax", "secure": secure}


@router.get(SSO_PATH, include_in_schema=False, dependencies=[Depends(require_app_login)])
async def console_sso(request: Request):
    """Open the MinIO file manager already signed in.

    The console keeps its session in a cookie named `token`. Because the
    console is served from this same origin under /minio, the pipeline can
    log in to MinIO on the operator's behalf, hand that cookie to the browser,
    and send them straight to the bucket. Nobody needs the MinIO password.
    """
    target = f"{PREFIX}/browser/{quote(config.MINIO_BUCKET, safe='')}"
    if not config.MINIO_ACCESS_KEY or not config.MINIO_SECRET_KEY:
        return RedirectResponse(f"{PREFIX}/", status_code=303)
    try:
        upstream = await _client.post(
            "/api/v1/login",
            json={"accessKey": config.MINIO_ACCESS_KEY, "secretKey": config.MINIO_SECRET_KEY},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        log.warning("MinIO console login failed: %s", exc)
        return RedirectResponse(f"{PREFIX}/", status_code=303)
    token = upstream.cookies.get("token")
    if upstream.status_code >= 300 or not token:
        log.warning("MinIO console login rejected (%s)", upstream.status_code)
        return RedirectResponse(f"{PREFIX}/", status_code=303)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("token", token, max_age=12 * 3600, **_console_cookie_attrs(request))
    return response


def clear_console_cookie(response: Response) -> None:
    """Drop the MinIO session when the pipeline session ends."""
    response.delete_cookie("token", path="/")


@router.get(PREFIX, include_in_schema=False, dependencies=[Depends(require_app_login)])
def console_root():
    return RedirectResponse(SSO_PATH, status_code=303)


@router.api_route(
    PREFIX + "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
    dependencies=[Depends(require_app_login)],
)
async def console_http(path: str, request: Request):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    if path.startswith("static/js/main."):
        headers.pop("accept-encoding", None)
    upstream_request = _client.build_request(
        request.method,
        _upstream_path(path, request.url.query),
        headers=headers,
        content=request.stream(),
    )
    upstream = await _client.send(upstream_request, stream=True)

    # The console builds its file-list socket as wss://host:<port>/... and, on
    # the public site, <port> is empty. The browser then opens
    # wss://host:/minio/ws/... and does not attach the login cookie, so MinIO
    # answers 403 and the bucket looks empty even though the file is there.
    if request.method == "GET" and path.startswith("static/js/main.") and path.endswith(".js"):
        body = (await upstream.aread()).replace(
            b'"".concat(h,"://").concat(n.hostname,":").concat(c).concat(p,"ws/objectManager")',
            b'"".concat(h,"://").concat(n.hostname).concat(c?":".concat(c):"").concat(p,"ws/objectManager")',
        )
        await upstream.aclose()
        headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}
        }
        headers["content-length"] = str(len(body))
        return Response(content=body, status_code=upstream.status_code, headers=headers)

    response_headers = [
        (k, v) for k, v in upstream.headers.multi_items() if k.lower() not in HOP_BY_HOP
    ]
    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    response.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in response_headers
    ]
    return response


@router.websocket(PREFIX + "/{path:path}")
async def console_ws(path: str, websocket: WebSocket):
    base = config.MINIO_CONSOLE_INTERNAL_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    target = base.rstrip("/") + _upstream_path(path, websocket.url.query)
    forwarded = {}
    if cookie := websocket.headers.get("cookie"):
        forwarded["Cookie"] = cookie

    if not _signed_in(websocket.scope.get("session") or {}):
        await websocket.close(code=1008, reason="Sign in first.")
        return

    await websocket.accept()
    try:
        async with ws_connect(target, additional_headers=forwarded, max_size=None) as upstream:
            async def client_to_upstream():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close()
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_client():
                async for data in upstream:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except (ConnectionClosed, OSError) as exc:
        log.info("MinIO console websocket closed: %s", exc)
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
