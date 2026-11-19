import asyncio
import hashlib
import logging
import secrets
from datetime import timedelta
from uuid import UUID

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 30 * 60  # emailed links are short-lived; the session cookie is the long-lived thing


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue(
    conn: asyncpg.Connection, observer_id: UUID, ttl_s: int = DEFAULT_TTL_S, *, purpose: str = "login"
) -> str:
    """Mint a single-use token. Returns the raw token for the URL; only its
    SHA-256 is persisted, so a database leak yields nothing usable.

    `purpose` is what the link may do: "login" signs in, "delete" confirms
    deleting the account (0032). Neither can be used as the other."""
    raw = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO login_tokens (token_hash, observer_id, expires_at, purpose) "
        "VALUES ($1, $2, now() + make_interval(secs => $3), $4)",
        _hash(raw),
        observer_id,
        ttl_s,
        purpose,
    )
    return raw


async def peek(conn: asyncpg.Connection, raw: str, *, purpose: str) -> UUID | None:
    """The observer a still-usable token is for, without using it up. For a
    page that shows what clicking through will do before anything is done."""
    if not raw:
        return None
    return await conn.fetchval(
        "SELECT observer_id FROM login_tokens WHERE token_hash = $1 AND purpose = $2 "
        "AND used_at IS NULL AND expires_at > now()",
        _hash(raw), purpose)


async def consume(conn: asyncpg.Connection, raw: str, *, purpose: str = "login") -> UUID | None:
    """Burn a token and return its observer, or None if it is unknown,
    expired, or already used.

    Single statement on purpose: the UPDATE ... WHERE used_at IS NULL is what
    makes single-use atomic. Checking then updating would let two concurrent
    clicks both succeed.
    """
    if not raw:
        return None
    return await conn.fetchval(
        """
        UPDATE login_tokens SET used_at = now()
        WHERE token_hash = $1 AND purpose = $2 AND used_at IS NULL AND expires_at > now()
        RETURNING observer_id
        """,
        _hash(raw),
        purpose,
    )


async def purge_expired(conn: asyncpg.Connection) -> int:
    """Bound history for both token purposes, used or unused (#209).

    Retention starts at expiry, not consumption. Never delete an unexpired
    link; deleting a used row does not affect the session it established.
    The existing expires_at index serves this sweep without a migration.
    """
    result = await conn.execute(
        "DELETE FROM login_tokens WHERE expires_at <= now() - $1::interval",
        timedelta(days=settings.login_token_retention_days),
    )
    return int(result.split()[-1])


async def consumer(pool: asyncpg.Pool) -> None:
    """Sweep immediately at startup, then periodically while the API runs."""
    while True:
        try:
            async with pool.acquire() as conn:
                deleted = await purge_expired(conn)
            if deleted:
                logger.info("expired token rows removed: count=%s", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A database exception can include row values. Keep token hashes
            # and observer links out of logs; the next pass retries the work.
            logger.warning("token housekeeping failed (%s); will retry", type(exc).__name__)
        await asyncio.sleep(settings.login_token_cleanup_interval_s)
