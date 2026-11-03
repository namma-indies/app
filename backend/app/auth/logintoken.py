import hashlib
import secrets
from uuid import UUID

import asyncpg

DEFAULT_TTL_S = 30 * 60  # emailed links are short-lived; the session cookie is the long-lived thing


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue(
    conn: asyncpg.Connection, observer_id: UUID, ttl_s: int = DEFAULT_TTL_S, *, purpose: str = "login",
    signup_consent: str | None = None,
) -> str:
    """Mint a single-use token. Returns the raw token for the URL; only its
    SHA-256 is persisted, so a database leak yields nothing usable.

    `purpose` is what the link may do: "login" signs in, "delete" confirms
    deleting the account (0032). Neither can be used as the other."""
    raw = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO login_tokens (token_hash, observer_id, expires_at, purpose, signup_consent) "
        "VALUES ($1, $2, now() + make_interval(secs => $3), $4, $5::jsonb)",
        _hash(raw),
        observer_id,
        ttl_s,
        purpose,
        signup_consent,
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


async def consume_for_signin(conn: asyncpg.Connection, raw: str):
    """Claim a login token with its pending agreement, atomically and once.

    The caller commits redemption and the verified account receipt together.
    Delete-purpose tokens can never record agreement or sign anyone in.
    """
    if not raw:
        return None
    return await conn.fetchrow(
        """UPDATE login_tokens SET used_at = now()
           WHERE token_hash=$1 AND purpose='login' AND used_at IS NULL AND expires_at > now()
           RETURNING observer_id, signup_consent""",
        _hash(raw),
    )
