import hashlib
import secrets
from uuid import UUID

import asyncpg

DEFAULT_TTL_S = 30 * 60  # emailed links are short-lived; the session cookie is the long-lived thing


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue(
    conn: asyncpg.Connection, observer_id: UUID, ttl_s: int = DEFAULT_TTL_S
) -> str:
    """Mint a single-use login token. Returns the raw token for the URL; only
    its SHA-256 is persisted, so a database leak yields nothing usable."""
    raw = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO login_tokens (token_hash, observer_id, expires_at) "
        "VALUES ($1, $2, now() + make_interval(secs => $3))",
        _hash(raw),
        observer_id,
        ttl_s,
    )
    return raw


async def consume(conn: asyncpg.Connection, raw: str) -> UUID | None:
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
        WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now()
        RETURNING observer_id
        """,
        _hash(raw),
    )
