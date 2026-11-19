"""Count handled sign-in failures, on the handler's own connection when it holds one."""
import logging
from functools import wraps

from fastapi import HTTPException

from app.abuse import record

logger = logging.getLogger(__name__)


def audit_failures(handler):
    @wraps(handler)
    async def counted(*args, **kwargs):
        async def note(status):
            # Only a refused credential is a failed sign-in: a wrong
            # passcode, reviewer code or link (401). Malformed input,
            # allowlist misses, consent prompts, a disabled door and provider
            # trouble are not, and would make the spike signal cry wolf.
            if status not in (401, 429):
                return
            try:
                event = "failed_signins" if status == 401 else "auth_throttled"
                if "conn" in kwargs:
                    await record(kwargs["conn"], event)
                else:
                    # A pool-based handler has already handed its connection back.
                    async with kwargs["request"].app.state.pool.acquire() as conn:
                        await record(conn, event)
            except Exception:
                logger.warning("sign-in audit counter unavailable")
        try:
            response = await handler(*args, **kwargs)
        except HTTPException as exc:
            await note(exc.status_code)
            raise
        await note(response.status_code)
        return response
    return counted
