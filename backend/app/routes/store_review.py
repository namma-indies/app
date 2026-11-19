"""Explicit, default-off store reviewer sign-in; no real inbox is required."""
import hmac
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from app.abuse import record
from app.auth import store_review
from app.auth.audit import audit_failures
from app.auth.deps import set_session_cookie
from app.config import settings
from app.deps import get_conn
from app.ratelimit import Limit, check, check_backoff, clear_failures, client_key, record_failure

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/auth/options")
async def auth_options():
    return JSONResponse({"store_reviewer_enabled": store_review.enabled()}, headers={"Cache-Control": "no-store"})


@router.post("/auth/reviewer")
@audit_failures
async def reviewer_signin(request: Request, secret: str = Form(..., max_length=512), conn=Depends(get_conn)):
    if not store_review.enabled():
        raise HTTPException(404, "not found")
    key = f"store-review:ip:{client_key(request)}"
    try:
        await check(conn, key, Limit(settings.rl_store_review_times, settings.rl_store_review_window_s))
        await check_backoff(conn, key)
        if not hmac.compare_digest(secret.encode(), store_review.secret().encode()):
            await record_failure(conn, key)
            raise HTTPException(401, "That reviewer code is not valid. Check the review notes and try again.")
        oid = await store_review.account(conn)
    except HTTPException:
        logger.info("store_review_signin outcome=failed")
        raise
    await clear_failures(conn, key)
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    set_session_cookie(response, oid, max_age_s=store_review.MAX_AGE_S)
    store_review.set_cookie(response, oid)
    await record(conn, "reviewer_signins")
    logger.info("store_review_signin outcome=success observer=%s", oid)
    return response
