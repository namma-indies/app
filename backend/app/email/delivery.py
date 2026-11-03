"""Shared SES admission and bounded, persisted retry backoff.

Only attempt metadata is persisted. Raw login tokens/links never enter a queue.
The budget counts reservations, including failed/ambiguous attempts, to stay
conservative if a worker dies after SES accepts a message.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError,
                                 EndpointConnectionError, ReadTimeoutError)
from fastapi import HTTPException

from app.abuse import record
from app.config import settings
from app.ids import uuid7
from app.ratelimit import throttled

logger = logging.getLogger(__name__)
TRANSIENT = {"TooManyRequestsException", "Throttling", "ThrottlingException", "ServiceUnavailable",
             "ServiceUnavailableException", "InternalFailure", "InternalServerError"}


def scope() -> str:
    return f"ses:{settings.ses_region}"


async def reserve(conn):
    if settings.email_sender != "ses":
        return None
    async with conn.transaction():
        await conn.execute("INSERT INTO email_delivery_state (scope) VALUES ($1) ON CONFLICT DO NOTHING", scope())
        state = await conn.fetchrow("""SELECT next_at, statement_timestamp() AS now
            FROM email_delivery_state WHERE scope=$1 FOR UPDATE""", scope())
        if state["next_at"] > state["now"]:
            raise throttled((state["next_at"] - state["now"]).total_seconds(), "Email delivery is busy.")
        usage = await conn.fetchrow("""SELECT count(*) AS count, min(attempted_at) AS first
            FROM email_send_attempts WHERE scope=$1 AND attempted_at > statement_timestamp()-interval '24 hours'""", scope())
        if usage["count"] >= settings.email_daily_budget:
            wait = (usage["first"] + timedelta(hours=24) - state["now"]).total_seconds()
            raise throttled(wait, "The email sending budget is full.")
        attempt_id = uuid7()
        await conn.execute("INSERT INTO email_send_attempts (id, scope, attempted_at) VALUES ($1,$2,statement_timestamp())", attempt_id, scope())
        await conn.execute("UPDATE email_delivery_state SET next_at=statement_timestamp()+make_interval(secs => $2) WHERE scope=$1",
                           scope(), settings.email_send_interval_s)
    return attempt_id


def _kind(exc: Exception) -> tuple[bool, bool, str]:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "")
        if code in TRANSIENT:
            # SES uses the same throttle family for daily and per-second caps.
            # Inspect the reason privately; never put it in logs or replies.
            daily = "daily" in error.get("Message", "").lower()
            return True, daily, "provider_throttled"
    if isinstance(exc, (ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)):
        return True, False, "transport_unavailable"
    return False, False, "provider_rejected"


async def _backoff(conn, daily: bool) -> int:
    async with conn.transaction():
        failures = await conn.fetchval("UPDATE email_delivery_state SET failures=least(failures+1,20) WHERE scope=$1 RETURNING failures", scope())
        delay = settings.email_quota_backoff_s if daily else min(settings.email_backoff_max_s, 2 ** (failures - 1))
        await conn.execute("""UPDATE email_delivery_state SET next_at=greatest(next_at,
            statement_timestamp()+make_interval(secs => $2)) WHERE scope=$1""", scope(), delay)
    return delay


async def deliver(conn, send: Callable[[], Awaitable[None]], *, reserved=None) -> None:
    if settings.email_sender != "ses":
        await send()
        return
    for attempt in range(settings.email_send_attempts):
        try:
            slot = reserved if attempt == 0 and reserved is not None else await reserve(conn)
        except HTTPException:
            await record(conn, "email_throttled")
            raise
        try:
            await send()
        except Exception as exc:
            transient, daily, category = _kind(exc)
            logger.warning("email_delivery outcome=%s attempt=%d", category, attempt + 1)
            if not transient:
                await record(conn, "email_failed")
                raise HTTPException(503, "We couldn't send that email. Please try again in 60 seconds or contact support.",
                                    headers={"Retry-After": "60"}) from None
            wait = await _backoff(conn, daily)
            await record(conn, "email_throttled")
            if attempt + 1 < settings.email_send_attempts and wait <= 4 and not daily:
                await asyncio.sleep(wait)
                continue
            raise throttled(wait, "The email provider is busy.") from None
        else:
            await conn.execute("UPDATE email_send_attempts SET accepted=true WHERE id=$1", slot)
            await conn.execute("UPDATE email_delivery_state SET failures=0 WHERE scope=$1", scope())
            return
