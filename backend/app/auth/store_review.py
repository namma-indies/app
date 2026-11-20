"""Revocable store-review credentials and the database-enforced demo identity."""
import hashlib
from uuid import UUID

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import settings
from app.ids import uuid7

COOKIE = "store_review"
MAX_AGE_S = 7 * 24 * 3600
_LOCK = int.from_bytes(hashlib.sha256(b"store-review-singleton-v1").digest()[:8], signed=True)


def secret() -> str:
    return settings.store_review_secret.get_secret_value()


def enabled() -> bool:
    value = secret()
    return settings.store_review_enabled and value.isascii() and 32 <= len(value) <= 512


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret(), salt="store-review-session-v1")


def set_cookie(response, observer_id: UUID) -> None:
    response.set_cookie(COOKIE, _signer().dumps(str(observer_id)), max_age=MAX_AGE_S,
                        httponly=True, secure=True, samesite="none", path="/")


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, httponly=True, secure=True, samesite="none", path="/")


def valid_cookie(raw: str | None, observer_id: UUID) -> bool:
    if not enabled() or not raw:
        return False
    try:
        return UUID(_signer().loads(raw, max_age=MAX_AGE_S)) == observer_id
    except (BadData, TypeError, ValueError):
        return False


async def account(conn) -> UUID:
    async with conn.transaction():
        # Covers the empty-table race too. The partial unique index is the
        # backstop even if another writer forgets this lock.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK)
        row = await conn.fetchrow("SELECT id, deleted_at, banned_at FROM observers WHERE store_reviewer FOR UPDATE")
        if row is not None:
            if row["deleted_at"] is not None or row["banned_at"] is not None:
                # Never mint a replacement to evade a ban or undo deletion.
                raise HTTPException(503, "The review account is unavailable. Please contact the app team.")
            return row["id"]
        oid = uuid7()
        await conn.execute("""INSERT INTO observers (id, display_name, created_via, store_reviewer)
            VALUES ($1, 'Store reviewer', 'store_review', true)""", oid)
        return oid


class ResetRefused(Exception):
    """The reset was not done; the message says why, with no personal data."""


async def reset(pool, storage, *, keep_content: bool = False, allow_banned: bool = False) -> str:
    """Bring the store-review door back after the review account was deleted
    (by the reviewer, in the app, as reviewers do) or otherwise spent.

    keep_content: only cancel a deletion that is still scheduled, keeping
    the account and what it logged. Otherwise the account's content is
    erased exactly as an account deletion erases it, its tombstone stops
    being the reviewer, and a fresh reviewer identity is created -- the next
    sign-in gets a clean demo account. Old reviewer cookies name the old id
    and stop working.

    A banned reviewer is refused unless allow_banned: a moderator's ban is a
    decision someone made, and undoing it should be deliberate.
    Returns a one-word outcome for the operator."""
    from app import accounts
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT o.id, o.deleted_at, o.banned_at, o.deletion_requested_at, d.status
               FROM observers o LEFT JOIN account_deletions d ON d.observer_id = o.id
               WHERE o.store_reviewer""")
    if row is not None and row["banned_at"] is not None and not allow_banned:
        raise ResetRefused("the review account is banned; pass --allow-banned to reset it anyway")
    if keep_content:
        if row is None or row["deleted_at"] is None and row["deletion_requested_at"] is None:
            return "unchanged"
        async with pool.acquire() as conn, conn.transaction():
            kept = await accounts.cancel(conn, row["id"])
        if not kept:
            raise ResetRefused("its deletion has already started or finished; run without --keep-content")
        return "kept"
    if row is None:
        async with pool.acquire() as conn:
            await account(conn)
        return "created"
    if row["deleted_at"] is None:
        # Stop the grace-period purge from racing this one; erasing now is
        # the same erasure, idempotent if a purge had already begun.
        async with pool.acquire() as conn:
            await conn.execute("""UPDATE account_deletions SET status='cancelled', cancelled_at=now()
                WHERE observer_id=$1 AND status='scheduled'""", row["id"])
        await accounts.erase_account(pool, storage, row["id"])
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK)
        await conn.execute("UPDATE observers SET store_reviewer=false, updated_at=now() WHERE id=$1", row["id"])
        await conn.execute("""INSERT INTO observers (id, display_name, created_via, store_reviewer)
            VALUES ($1, 'Store reviewer', 'store_review', true)""", uuid7())
    return "reset"
