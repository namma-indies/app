from uuid import UUID

from fastapi import Depends, HTTPException
from starlette.requests import Request

from app.deps import get_conn as _get_conn
from app.consent import CONSENT_REQUIRED, needs_consent
from app.security import issue_session, read_session

SESSION_COOKIE = "session"
SESSION_MAX_AGE_S = 60 * 60 * 24 * 400  # ~browser max; effectively non-expiring, survives PWA restarts


PENDING_DELETION = "account_pending_deletion"
SUSPENDED = "account_suspended"


def _session_observer(request: Request) -> UUID:
    cookie_value = request.cookies.get(SESSION_COOKIE)
    if cookie_value is None:
        raise HTTPException(status_code=401)
    observer_id = read_session(cookie_value)
    if observer_id is None:
        raise HTTPException(status_code=401)
    return observer_id


async def _account_state(request: Request) -> tuple[UUID, str | None]:
    """(observer, why it may only reach /account, if it may) for a session
    whose account still exists: banned by a moderator (#180), or deletion
    scheduled (#179).

    A session is a signed cookie that never expires, so signing it proves only
    that we issued it once. Whether the account behind it still exists is the
    database's to say, on every request: a deleted account's cookie is a 401
    from the moment it is deleted (#179), not whenever the phone forgets it.

    The connection is borrowed for this one query and given straight back,
    never held for the request: an upload streaming a clip for minutes must
    not keep a pool connection while it does (tests/test_upload_concurrency.py).
    """
    observer_id = _session_observer(request)
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT deleted_at, deletion_requested_at, banned_at FROM observers WHERE id = $1", observer_id)
        consent_missing = (row is not None and row["deleted_at"] is None
                           and row["banned_at"] is None and row["deletion_requested_at"] is None
                           and await needs_consent(conn, observer_id))
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(status_code=401)
    if row["banned_at"] is not None:
        return observer_id, SUSPENDED
    if row["deletion_requested_at"] is not None:
        return observer_id, PENDING_DELETION
    return observer_id, CONSENT_REQUIRED if consent_missing else None


async def require_observer(request: Request) -> UUID:
    """A signed-in observer whose account is in use.

    One whose account is scheduled for deletion gets 403
    `account_pending_deletion` everywhere but /account, where they can see
    when it goes and keep it (`require_account`). A banned one gets 403
    `account_suspended`, and may still reach /account to delete it."""
    observer_id, only_account = await _account_state(request)
    if only_account == CONSENT_REQUIRED:
        raise HTTPException(428, CONSENT_REQUIRED, headers={"Cache-Control": "no-store"})
    if only_account:
        raise HTTPException(status_code=403, detail=only_account)
    return observer_id


async def require_account(request: Request) -> UUID:
    """A signed-in observer, including one whose account is scheduled for
    deletion or suspended: for the routes that show, schedule and cancel
    deletion. Deleting your account is yours to do even when banned."""
    observer_id, _ = await _account_state(request)
    return observer_id


async def require_moderator(
    observer_id: UUID = Depends(require_observer), conn=Depends(_get_conn)
) -> UUID:
    """A 404, not a 403, for a non-moderator.

    403 confirms the endpoint exists and that this account simply lacks the
    tier, which turns a privileged surface into something to probe for. There
    is nothing here worth revealing to someone who cannot use it.

    Lives here rather than in a route module because it now gates two
    unrelated surfaces -- the moderation queue and the stats dashboard -- and
    a route importing a dependency from another route is a tangle waiting to
    happen. Grant it the same way:

        UPDATE observers SET trust_tier = 'moderator' WHERE email = '...';
    """
    tier = await conn.fetchval(
        "SELECT trust_tier FROM observers WHERE id = $1 AND deleted_at IS NULL",
        observer_id,
    )
    if tier != "moderator":
        raise HTTPException(status_code=404, detail="not found")
    return observer_id


def set_session_cookie(response, observer_id: UUID) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(observer_id),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        # "none" (not "lax"): the native iOS app's webview is a genuinely
        # different origin from the API, and Lax withholds the cookie on
        # cross-origin fetches even with credentials included. Requires
        # secure=True, already set below.
        samesite="none",
        path="/",
        secure=True,
    )


def clear_session_cookie(response) -> None:
    # Must mirror set_session_cookie's attributes. A delete only matches a
    # cookie whose path/secure/samesite agree -- get them wrong and the browser
    # keeps the original quietly, so logout appears to work and doesn't.
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="none",
        secure=True,
    )
