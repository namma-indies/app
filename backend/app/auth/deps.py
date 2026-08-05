from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request

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
