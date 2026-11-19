"""Postgres-backed budgets, cooldowns and progressive authentication backoff.

Every decision uses the database clock and an atomic upsert. No process-local
counter can be reset by deploying or multiplied by adding uvicorn workers.
Keys are domain-separated HMACs: neither addresses nor IPs are stored here.
"""
import asyncio
import hashlib
import hmac
import logging
import math
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    times: int
    window_s: int


def key_hash(key: str) -> bytes:
    return hmac.digest(settings.phone_hash_secret.encode(), f"rate-limit:{key}".encode(), hashlib.sha256)


def client_key(request: Request) -> str:
    """Only trust the header Caddy overwrites, and only in that topology."""
    if settings.trust_proxy_header:
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def throttled(seconds: float, message: str = "Too many requests.") -> HTTPException:
    wait = max(1, math.ceil(seconds))
    return HTTPException(429, f"{message} Try again in {wait} seconds.", headers={"Retry-After": str(wait)})


async def cleanup(conn) -> None:
    # Bounded, indexed sweeps; live budgets are never evicted to make room.
    for table in ("rate_limit_buckets", "auth_failures", "join_pow_uses"):
        await conn.execute(f"""DELETE FROM {table} WHERE ctid IN (
            SELECT ctid FROM {table} WHERE expires_at <= statement_timestamp()
            ORDER BY expires_at LIMIT 64 FOR UPDATE SKIP LOCKED
        )""")


async def sweeper(pool) -> None:
    """Also expire idle state when nobody is making new requests."""
    while True:
        try:
            async with pool.acquire() as conn:
                await cleanup(conn)
        except Exception:
            logger.warning("rate-limit cleanup failed; will retry")
        await asyncio.sleep(60)


async def check(conn, key: str, limit: Limit, *, amount: int = 1) -> None:
    if not settings.rate_limit_enabled:
        return
    if limit.times < 1 or limit.window_s < 1 or amount < 1:
        raise ValueError("rate limits and amounts must be positive")
    await cleanup(conn)
    row = await conn.fetchrow("""
        INSERT INTO rate_limit_buckets (key_hash, hits, expires_at)
        VALUES ($1, $3, to_timestamp((floor(extract(epoch FROM statement_timestamp()) / $2) + 1) * $2))
        ON CONFLICT (key_hash) DO UPDATE SET
            hits = CASE WHEN rate_limit_buckets.expires_at <= statement_timestamp()
                THEN $3 ELSE least(rate_limit_buckets.hits + $3, $4::bigint + $3) END,
            expires_at = CASE WHEN rate_limit_buckets.expires_at <= statement_timestamp()
                THEN EXCLUDED.expires_at ELSE rate_limit_buckets.expires_at END
        RETURNING hits, extract(epoch FROM expires_at - statement_timestamp()) AS wait
    """, key_hash(f"window:{limit.window_s}:{key}"), limit.window_s, amount, limit.times)
    if row["hits"] > limit.times:
        raise throttled(row["wait"])


async def cooldown(conn, key: str, seconds: float) -> None:
    """One admission per rolling interval; rejected calls do not extend it."""
    if not settings.rate_limit_enabled:
        return
    row = await conn.fetchrow("""
        INSERT INTO rate_limit_buckets (key_hash, hits, expires_at)
        VALUES ($1, 1, statement_timestamp() + make_interval(secs => $2))
        ON CONFLICT (key_hash) DO UPDATE SET
            hits = CASE WHEN rate_limit_buckets.expires_at <= statement_timestamp() THEN 1 ELSE 2 END,
            expires_at = CASE WHEN rate_limit_buckets.expires_at <= statement_timestamp()
                THEN EXCLUDED.expires_at ELSE rate_limit_buckets.expires_at END
        RETURNING hits, extract(epoch FROM expires_at - statement_timestamp()) AS wait
    """, key_hash(f"cooldown:{key}"), float(seconds))
    if row["hits"] != 1:
        raise throttled(row["wait"], "Please wait before requesting another link.")


async def check_backoff(conn, key: str) -> None:
    if not settings.rate_limit_enabled:
        return
    wait = await conn.fetchval("""SELECT extract(epoch FROM blocked_until - statement_timestamp())
        FROM auth_failures WHERE key_hash = $1 AND expires_at > statement_timestamp()
        AND blocked_until > statement_timestamp()""", key_hash(key))
    if wait is not None:
        raise throttled(wait, "Too many failed sign-in attempts.")


async def record_failure(conn, key: str) -> None:
    """Record even the failure starting a lockout; commit before raising."""
    if not settings.rate_limit_enabled:
        return
    async with conn.transaction():
        failures = await conn.fetchval("""
            INSERT INTO auth_failures (key_hash, failures, blocked_until, expires_at)
            VALUES ($1, 1, statement_timestamp(), statement_timestamp() + make_interval(secs => $2))
            ON CONFLICT (key_hash) DO UPDATE SET
                failures = CASE WHEN auth_failures.expires_at <= statement_timestamp()
                    THEN 1 ELSE least(auth_failures.failures + 1, 100) END,
                expires_at = EXCLUDED.expires_at
            RETURNING failures
        """, key_hash(key), settings.auth_failure_window_s)
        delay = 0 if failures < settings.auth_backoff_after else min(
            settings.auth_backoff_max_s,
            settings.auth_backoff_base_s * 2 ** min(20, failures - settings.auth_backoff_after))
        await conn.execute("""UPDATE auth_failures
            SET blocked_until = statement_timestamp() + make_interval(secs => $2)
            WHERE key_hash = $1""", key_hash(key), delay)
    if delay:
        raise throttled(delay, "Too many failed sign-in attempts.")


async def clear_failures(conn, key: str) -> None:
    await conn.execute("DELETE FROM auth_failures WHERE key_hash = $1", key_hash(key))


def stats_limit() -> Limit:
    return Limit(settings.rl_stats_times, settings.rl_stats_window_s)


def voice_note_limit() -> Limit:
    return Limit(settings.rl_voice_note_times, settings.rl_voice_note_window_s)


def block_limit() -> Limit:
    return Limit(settings.rl_block_times, settings.rl_block_window_s)


def email_addr_limit() -> Limit:
    return Limit(settings.rl_email_addr_times, settings.rl_email_addr_window_s)


def email_ip_limit() -> Limit:
    return Limit(settings.rl_email_ip_times, settings.rl_email_ip_window_s)


def join_limit() -> Limit:
    return Limit(settings.rl_join_times, settings.rl_join_window_s)
