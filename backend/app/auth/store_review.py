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
