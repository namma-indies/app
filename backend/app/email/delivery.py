"""Shared SES admission and bounded, persisted retry backoff.

Only attempt metadata is persisted. Raw login tokens/links never enter a queue.
The budget counts reservations, including failed/ambiguous attempts, to stay
conservative if a worker dies after SES accepts a message.

Three controls, all in Postgres so every worker shares them:

- A token bucket for the provider's per-second rate. A send that finds the
  bucket briefly empty is given a short wait (served outside any transaction
  and without a connection) rather than an immediate "busy".
- A rolling 24-hour budget, split by purpose. Sign-in mail may use all of
  it; account mail (deletion links and notices) is capped separately and can
  never eat into the share held back for sign-in.
- A shared pause, set when SES says to stop: a short exponential one for
  throttling and transport trouble, a long one for quota and account-level
  refusals, so no worker keeps calling a provider that has said no.
"""
import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError,
                                 EndpointConnectionError, ReadTimeoutError)
from fastapi import HTTPException

from app.abuse import record
from app.config import settings
from app.ids import uuid7
from app.ratelimit import throttled

logger = logging.getLogger(__name__)

PURPOSES = ("signin", "account")

# SESv2 SendEmail errors (API reference, "Errors"), plus the common throttling
# and server-side codes every AWS API can return. Classified by code only;
# the message text is neither inspected nor logged.
TRANSIENT = {"TooManyRequestsException", "Throttling", "ThrottlingException", "ServiceUnavailable",
             "ServiceUnavailableException", "InternalFailure", "InternalServerError"}
# The 24-hour sending quota is spent: nothing will send until it rolls.
QUOTA = {"LimitExceededException"}
# The account cannot send at all until a human acts: suspended, paused (by
# AWS or by us), or the MAIL FROM domain lost its verification.
ACCOUNT = {"AccountSuspendedException", "SendingPausedException", "MailFromDomainNotVerifiedException"}
# Everything else (MessageRejected, BadRequestException, NotFoundException,
# unknown codes) is about this one message: not retried, no shared pause.


@dataclass(frozen=True)
class Slot:
    id: UUID
    wait_s: float


def scope() -> str:
    return f"ses:{settings.ses_region}"


def signin_floor() -> int:
    """The part of the daily budget only sign-in mail may use."""
    return int(settings.email_daily_budget * settings.email_signin_floor_fraction)


async def _check_budget(conn, purpose: str, now) -> None:
    usage = await conn.fetchrow("""SELECT count(*) AS total, min(attempted_at) AS first,
            count(*) FILTER (WHERE purpose = $2) AS mine, min(attempted_at) FILTER (WHERE purpose = $2) AS first_mine
        FROM email_send_attempts WHERE scope=$1 AND attempted_at > statement_timestamp()-interval '24 hours'""",
        scope(), purpose)
    if usage["total"] >= settings.email_daily_budget:
        raise throttled((usage["first"] + timedelta(hours=24) - now).total_seconds(),
                        "The email sending budget is full.")
    if purpose != "signin" and (usage["mine"] >= settings.email_account_daily_budget
                                or usage["total"] >= settings.email_daily_budget - signin_floor()):
        first = usage["first_mine"] or usage["first"]
        raise throttled((first + timedelta(hours=24) - now).total_seconds(), "The email sending budget is full.")


def _refill(state) -> tuple[float, float]:
    """Tokens available now (may be negative: each unit below zero is a send
    already scheduled 1/rate later) and how long a new send must wait."""
    rate = settings.email_send_rate_per_s
    elapsed = max(0.0, (state["now"] - state["refilled_at"]).total_seconds())
    tokens = min(float(settings.email_send_burst), state["tokens"] + elapsed * rate)
    return tokens, max(0.0, (1 - tokens) / rate)


async def _state(conn, *, lock: bool):
    await conn.execute("INSERT INTO email_delivery_state (scope) VALUES ($1) ON CONFLICT DO NOTHING", scope())
    return await conn.fetchrow(f"""SELECT next_at, tokens, refilled_at, statement_timestamp() AS now
        FROM email_delivery_state WHERE scope=$1 {'FOR UPDATE' if lock else ''}""", scope())


def _admit(state) -> float:
    if state["next_at"] > state["now"]:
        raise throttled((state["next_at"] - state["now"]).total_seconds(), "Email delivery is busy.")
    _, wait = _refill(state)
    if wait > settings.email_send_max_wait_s:
        raise throttled(wait, "Email delivery is busy.")
    return wait


async def check_capacity(conn, purpose: str = "signin") -> None:
    """Would `reserve` admit a send right now? Spends nothing.

    For flows that must answer identically whether or not an account uses an
    address: everyone gets the same admission answer, and only a real send
    spends real budget."""
    if settings.email_sender != "ses":
        return
    async with conn.transaction():
        state = await _state(conn, lock=False)
        _admit(state)
        await _check_budget(conn, purpose, state["now"])


async def reserve(conn, purpose: str = "signin") -> Slot | None:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown email purpose {purpose!r}")
    if settings.email_sender != "ses":
        return None
    async with conn.transaction():
        state = await _state(conn, lock=True)
        wait = _admit(state)
        await _check_budget(conn, purpose, state["now"])
        tokens, _ = _refill(state)
        attempt_id = uuid7()
        await conn.execute("""INSERT INTO email_send_attempts (id, scope, attempted_at, purpose)
            VALUES ($1,$2,statement_timestamp(),$3)""", attempt_id, scope(), purpose)
        await conn.execute("UPDATE email_delivery_state SET tokens=$2, refilled_at=$3 WHERE scope=$1",
                           scope(), tokens - 1, state["now"])
    return Slot(attempt_id, wait)


def _kind(exc: Exception) -> tuple[str, str]:
    """(how to react, a fixed category for the log). Reactions: transient,
    quota, account, message."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in TRANSIENT:
            return "transient", "provider_throttled"
        if code in QUOTA:
            return "quota", "provider_quota_exhausted"
        if code in ACCOUNT:
            # A fixed AWS vocabulary, safe to log; it tells the operator what to fix.
            return "account", f"provider_account_blocked code={code}"
    if isinstance(exc, (ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)):
        return "transient", "transport_unavailable"
    return "message", "provider_rejected"


async def _backoff(conn, reaction: str) -> int:
    async with conn.transaction():
        failures = await conn.fetchval("UPDATE email_delivery_state SET failures=least(failures+1,20) WHERE scope=$1 RETURNING failures", scope())
        delay = {"quota": settings.email_quota_backoff_s, "account": settings.email_account_pause_s}.get(
            reaction, min(settings.email_backoff_max_s, 2 ** (failures - 1)))
        await conn.execute("""UPDATE email_delivery_state SET next_at=greatest(next_at,
            statement_timestamp()+make_interval(secs => $2)) WHERE scope=$1""", scope(), delay)
    return delay


@contextlib.asynccontextmanager
async def _connection(db):
    """A pool hands out a connection for each step and takes it back, so no
    connection is held while waiting on the bucket, on SES or between retries.
    A bare connection (tests, callers that already hold one) is just used."""
    if hasattr(db, "acquire"):
        async with db.acquire() as conn:
            yield conn
    else:
        yield db


async def deliver(db, send: Callable[[], Awaitable[None]], *, purpose: str = "signin", reserved=None) -> None:
    """Send with admission, pacing and bounded retry. Pass the pool, not a
    connection, so nothing is held across the waits."""
    if settings.email_sender != "ses":
        await send()
        return
    for attempt in range(settings.email_send_attempts):
        try:
            if attempt == 0 and reserved is not None:
                slot = reserved
            else:
                async with _connection(db) as conn:
                    slot = await reserve(conn, purpose)
        except HTTPException:
            async with _connection(db) as conn:
                await record(conn, "email_throttled")
            raise
        if slot.wait_s > 0:
            await asyncio.sleep(slot.wait_s)
        try:
            await send()
        except Exception as exc:
            reaction, category = _kind(exc)
            log = logger.error if reaction == "account" else logger.warning
            log("email_delivery outcome=%s attempt=%d", category, attempt + 1)
            async with _connection(db) as conn:
                if reaction == "message":
                    await record(conn, "email_failed")
                    raise HTTPException(503, "We couldn't send that email. Please try again in 60 seconds or contact support.",
                                        headers={"Retry-After": "60"}) from None
                wait = await _backoff(conn, reaction)
                await record(conn, "email_failed" if reaction == "account" else "email_throttled")
            if reaction == "account":
                raise HTTPException(503, f"We couldn't send that email. Please try again in {wait} seconds or contact support.",
                                    headers={"Retry-After": str(wait)}) from None
            if reaction == "transient" and attempt + 1 < settings.email_send_attempts and wait <= 4:
                await asyncio.sleep(wait)
                continue
            raise throttled(wait, "The email provider is busy.") from None
        else:
            async with _connection(db) as conn:
                await conn.execute("UPDATE email_send_attempts SET accepted=true WHERE id=$1", slot.id)
                await conn.execute("UPDATE email_delivery_state SET failures=0 WHERE scope=$1", scope())
            return
