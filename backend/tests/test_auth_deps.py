import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from app.auth.deps import SESSION_COOKIE, require_observer, set_session_cookie
from app.ids import uuid7
from app.security import issue_session


def _req(cookie: str | None):
    headers = [(b"cookie", f"{SESSION_COOKIE}={cookie}".encode())] if cookie else []
    return Request({"type": "http", "headers": headers})


@pytest.mark.asyncio
async def test_require_observer_accepts_valid_session():
    oid = uuid7()
    got = await require_observer(_req(issue_session(oid)))
    assert got == oid


@pytest.mark.asyncio
async def test_require_observer_rejects_missing_and_bad():
    with pytest.raises(HTTPException) as e1:
        await require_observer(_req(None))
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException):
        await require_observer(_req("garbage"))


def test_session_cookie_is_cross_origin_capable():
    # The native iOS app's webview (capacitor://localhost) is a genuinely
    # different origin from the API -- a Lax cookie is withheld on
    # cross-origin fetches even with credentials: "include", so the app
    # would authenticate against the web PWA but never against itself.
    response = Response()
    set_session_cookie(response, uuid7())
    set_cookie = response.headers["set-cookie"]
    assert "samesite=none" in set_cookie.lower()
    assert "secure" in set_cookie.lower()
    assert "httponly" in set_cookie.lower()
