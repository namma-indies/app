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
        samesite="lax",
        path="/",
        secure=True,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
