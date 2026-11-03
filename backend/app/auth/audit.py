"""Count handled sign-in failures without taking a second pool connection."""
import logging
from functools import wraps

from fastapi import HTTPException

from app.abuse import record

logger = logging.getLogger(__name__)


def audit_failures(handler):
    @wraps(handler)
    async def counted(*args, **kwargs):
        async def note(status):
            if status < 400:
                return
            try:
                await record(kwargs["conn"], "failed_signins")
                if status == 429:
                    await record(kwargs["conn"], "auth_throttled")
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
