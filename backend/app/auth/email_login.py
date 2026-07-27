from uuid import UUID

import asyncpg

from app.ids import uuid7


async def get_or_create_observer_by_email(
    conn: asyncpg.Connection, *, email: str, display_name: str | None = None
) -> UUID:
    """Resolve an email to a stable observer, creating one on first sight.

    This is the fix for split identity: the passcode gate mints a new observer
    per join, so one person becomes many. An address is a real key, so we look
    up by it. An existing display_name is never overwritten -- the name someone
    chose is theirs, and a later sign-in shouldn't rename them.
    """
    existing = await conn.fetchval("SELECT id FROM observers WHERE email = $1", email)
    if existing is not None:
        return existing

    observer_id = uuid7()
    name = (display_name or "").strip() or email.partition("@")[0]
    # ON CONFLICT covers two simultaneous first-time sign-ins racing on the
    # unique index; the loser reads back the winner's row.
    row = await conn.fetchrow(
        """
        INSERT INTO observers (id, email, display_name, created_via)
        VALUES ($1, $2, $3, 'email')
        ON CONFLICT (email) WHERE email IS NOT NULL DO NOTHING
        RETURNING id
        """,
        observer_id,
        email,
        name,
    )
    if row is not None:
        return row["id"]
    return await conn.fetchval("SELECT id FROM observers WHERE email = $1", email)
