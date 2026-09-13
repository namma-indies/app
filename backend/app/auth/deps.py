from uuid import UUID

from fastapi import Depends, HTTPException
from starlette.requests import Request

from app.deps import get_conn as _get_conn
from app.security import issue_session, read_session

SESSION_COOKIE = "session"
SESSION_MAX_AGE_S = 60 * 60 * 24 * 400  # ~browser max; effectively non-expiring, survives PWA restarts


async def require_observer(request: Request) -> UUID:
    cookie_value = request.cookies.get(SESSION_COOKIE)
    if cookie_value is None:
        raise HTTPException(status_code=401)
    observer_id = read_session(cookie_value)
    if observer_id is None:
        raise HTTPException(status_code=401)
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
