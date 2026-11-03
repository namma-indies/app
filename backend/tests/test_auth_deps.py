import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from datetime import datetime, timezone

from app.auth.deps import SESSION_COOKIE, require_account, require_observer, set_session_cookie
from app.ids import uuid7
from app.security import issue_session


class _Pool:
    """Hands out a connection whose one query, the account's state, answers `row`."""
    def __init__(self, row):
        self.row = row

    def acquire(self):
        pool = self

        class _Acquired:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Acquired()

    async def fetchrow(self, sql, *args):
        return self.row


def _req(cookie: str | None, row=None):
    class _State:
        pool = _Pool(row if row is not None else ACTIVE)

    class _App:
        state = _State()
    headers = [(b"cookie", f"{SESSION_COOKIE}={cookie}".encode())] if cookie else []
    return Request({"type": "http", "headers": headers, "app": _App()})


ACTIVE = {
    "deleted_at": None,
    "deletion_requested_at": None,
    "banned_at": None,
    "store_reviewer": False,
}
NOW = datetime.now(timezone.utc)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ["missing", {**ACTIVE, "deleted_at": NOW}])
async def test_a_validly_signed_session_for_a_deleted_account_is_401(row):
    """The cookie never expires; the account behind it can (#179)."""
    class _Missing(_Pool):
        async def fetchrow(self, sql, *args):
            return None
    for dep in (require_observer, require_account):
        request = _req(issue_session(uuid7()), row if row != "missing" else None)
        if row == "missing":
            request.app.state.pool = _Missing(None)
        with pytest.raises(HTTPException) as e:
            await dep(request)
        assert e.value.status_code == 401


@pytest.mark.asyncio
async def test_an_account_pending_deletion_reaches_only_the_account_routes():
    oid, pending = uuid7(), {**ACTIVE, "deletion_requested_at": NOW}
    with pytest.raises(HTTPException) as e:
        await require_observer(_req(issue_session(oid), pending))
    assert (e.value.status_code, e.value.detail) == (403, "account_pending_deletion")
    assert await require_account(_req(issue_session(oid), pending)) == oid


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


@pytest.mark.asyncio
async def test_a_banned_account_is_suspended_but_can_still_delete_itself():
    oid, banned = uuid7(), {**ACTIVE, "banned_at": NOW}
    with pytest.raises(HTTPException) as e:
        await require_observer(_req(issue_session(oid), banned))
    assert (e.value.status_code, e.value.detail) == (403, "account_suspended")
    assert await require_account(_req(issue_session(oid), banned)) == oid
