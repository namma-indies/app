from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import RedirectResponse

from app.auth.deps import set_session_cookie
from app.auth.magiclink import consume
from app.auth.audit import audit_failures
from app.deps import get_conn
from app.ratelimit import check_backoff, clear_failures, client_key, record_failure

router = APIRouter()


@router.get("/auth/magic-link/consume")
@audit_failures
async def consume_link(request: Request, token: str = Query(max_length=1024), conn=Depends(get_conn)):
    failure_key = f"consume:ip:{client_key(request)}"
    await check_backoff(conn, failure_key)
    oid = await consume(conn, token)
    if oid is None:
        await record_failure(conn, failure_key)
        raise HTTPException(status_code=401, detail="invalid or expired link")
    await clear_failures(conn, failure_key)
    resp = RedirectResponse(url="/", status_code=303)
    set_session_cookie(resp, oid)
    return resp
