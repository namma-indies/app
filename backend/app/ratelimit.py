"""Postgres-backed budgets, cooldowns and progressive authentication backoff.

Every decision uses the database clock and an atomic upsert. No process-local
counter can be reset by deploying or multiplied by adding uvicorn workers.
Keys are domain-separated HMACs: neither addresses nor IPs are stored here.
"""
import asyncio
import hashlib
import hmac
import ipaddress
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


def _network(host: str) -> str:
    """One key per IPv4 address, one per IPv6 /64.

    A single IPv6 subscriber is routinely handed a whole /64, so keying the
    full address would give one caller 2**64 independent budgets."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def client_key(request: Request) -> str:
    """Only trust the header Caddy overwrites, and only in that topology."""
    if settings.trust_proxy_header:
        real = request.headers.get("x-real-ip")
        if real:
            return _network(real.strip())
    return _network(request.client.host) if request.client else "unknown"


def throttled(seconds: float, message: str = "Too many requests.") -> HTTPException:
    wait = max(1, math.ceil(seconds))
    return HTTPException(429, f"{message} Try again in {wait} seconds.", headers={"Retry-After": str(wait)})


async def cleanup(conn) -> int:
    """One bounded, indexed sweep; returns how many rows went. Live budgets
    are never evicted to make room."""
    swept = 0
    for table in ("rate_limit_buckets", "auth_failures", "join_pow_uses"):
        status = await conn.execute(f"""DELETE FROM {table} WHERE ctid IN (
            SELECT ctid FROM {table} WHERE expires_at <= statement_timestamp()
            ORDER BY expires_at LIMIT 64 FOR UPDATE SKIP LOCKED
        )""")
        swept += int(status.split()[-1])
    return swept


async def sweeper(pool) -> None:
    """The only place expired state is deleted: requests never pay for it.
    Keeps sweeping while there is a backlog, so a burst of one-off keys
    cannot outgrow a once-a-minute pass."""
    while True:
        try:
            async with pool.acquire() as conn:
                for _ in range(100):
                    if not await cleanup(conn):
                        break
        except Exception:
            logger.warning("rate-limit cleanup failed; will retry")
        await asyncio.sleep(60)


async def check(conn, key: str, limit: Limit, *, amount: int = 1) -> int:
    """Charge `amount` against the window; returns the hits now counted.

    Expired rows are reset in place by the upsert and swept by `sweeper`, so
    a request never pays for housekeeping."""
    if not settings.rate_limit_enabled:
        return 0
    if limit.times < 1 or limit.window_s < 1 or amount < 1:
        raise ValueError("rate limits and amounts must be positive")
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
    return row["hits"]


async def refund(conn, key: str, limit: Limit, *, amount: int = 1) -> None:
    """Give back a charge that turned out not to count (a correct passcode, an
    upload replay). Charging first and refunding after keeps concurrent
    requests bounded by the budget at every instant."""
    if not settings.rate_limit_enabled:
        return
    await conn.execute("""UPDATE rate_limit_buckets SET hits = greatest(hits - $2, 0)
        WHERE key_hash = $1 AND expires_at > statement_timestamp()""",
        key_hash(f"window:{limit.window_s}:{key}"), amount)


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


async def record_failure(conn, key: str, *, after: int | None = None) -> None:
    """Record even the failure starting a lockout; commit before raising.

    `after` is how many failures are free before backoff starts. Keys that are
    an IP address pass `settings.auth_ip_backoff_after`: one carrier NAT is
    many people, and a low threshold there punishes all of them."""
    if not settings.rate_limit_enabled:
        return
    after = settings.auth_backoff_after if after is None else after
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
        delay = 0 if failures < after else min(
            settings.auth_backoff_max_s,
            settings.auth_backoff_base_s * 2 ** min(20, failures - after))
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


def join_global_limit() -> Limit:
    return Limit(settings.rl_join_global_wrong_times, settings.rl_join_global_wrong_window_s)


def join_success_limit() -> Limit:
    return Limit(settings.rl_join_success_ip_day_times, 86400)


def consume_limit() -> Limit:
    return Limit(settings.rl_consume_times, settings.rl_consume_window_s)


def email_ip_day_limit() -> Limit:
    return Limit(settings.rl_email_ip_day_times, 86400)


def delete_request_limit() -> Limit:
    return Limit(settings.rl_delete_request_day_times, 86400)


# An upload route says "this was a replay of something already accepted" so
# the upload guard can give the budget back: retrying a queued photo must not
# cost the person twice. Routes with the request mark its state; routes
# without one set this header, which the guard strips before replying.
REPLAY_HEADER = "x-upload-replay"


def mark_replay(request) -> None:
    request.state.upload_replay = True
