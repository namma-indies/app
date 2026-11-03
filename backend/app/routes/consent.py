from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse

from app.auth.deps import require_account
from app.consent import (ConsentSubmission, current_acceptance, current_policy,
                         record_snapshot, validate_submission)
from app.deps import get_conn

router = APIRouter()


@router.get("/auth/consent")
async def policies():
    return JSONResponse(current_policy(), headers={"Cache-Control": "no-store"})


async def status(conn, observer_id: UUID) -> dict:
    policy = current_policy()
    accepted_at = await current_acceptance(conn, observer_id)
    return {
        "policy": policy,
        "required": policy["enabled"] and accepted_at is None,
        "accepted_at": accepted_at.isoformat() if accepted_at else None,
    }


@router.get("/account/consent")
async def account_consent(observer_id: UUID = Depends(require_account), conn=Depends(get_conn)):
    return JSONResponse(await status(conn, observer_id), headers={"Cache-Control": "no-store"})


@router.post("/account/consent")
async def accept_consent(request: Request, body: ConsentSubmission,
                         observer_id: UUID = Depends(require_account), conn=Depends(get_conn)):
    # SameSite=None is needed by the native app. Strict JSON prevents a
    # cross-site HTML form or a simple no-content-type request consenting
    # with someone else's session cookie. Browser JSON requires CORS preflight.
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "application/json is required")
    snapshot = validate_submission(body)
    async with conn.transaction():
        await record_snapshot(conn, observer_id, snapshot, source="account")
    return JSONResponse(await status(conn, observer_id), headers={"Cache-Control": "no-store"})
