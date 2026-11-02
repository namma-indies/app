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


# Every column anywhere in the schema that points at an observer. A merge has
# to move all of them -- miss one and retiring the absorbed row orphans data.
_OBSERVER_REFS = (
    ("captures", "observer_id"),
    ("sightings", "observer_id"),
    ("individuals", "named_by"),
    ("individuals", "created_by_observer"),
    ("individual_names", "proposed_by"),
    ("individual_names", "resolved_by"),
    ("match_proposals", "resolved_by"),
    ("confirmations", "observer_id"),
    ("observers", "created_by_observer"),
)


async def absorb_passcode_observers(
    conn: asyncpg.Connection, *, target: UUID, email: str
) -> int:
    """Fold anonymous passcode observers who typed `email` as their name into
    the verified observer that owns that address. Returns how many were absorbed.

    Testers were told to put their work email in the passcode form's name field,
    which turns that string into a usable key -- so their pre-email sightings can
    follow them instead of being stranded under a throwaway identity.

    Two guards. Only `created_via='passcode'` rows with no email of their own are
    absorbable, so a verified identity is never swallowed by another. And the
    caller must invoke this at *link-consume* time, not at submit: typing an
    address proves nothing, clicking what was mailed to it proves control.

    The passcode is shared, so someone who types a colleague's address does get
    their sightings attributed to that colleague. Accepted for a closed staff
    pilot; revisit before the passcode door is opened wider.
    """
    # One transaction from the checks to the last update, holding the target's
    # row and every source's: an account scheduled for deletion is never
    # carried anywhere (#179), or its uploads would leave the purge that was
    # promised, under an id the purge no longer looks at; and nothing is
    # carried into one, which would be erased with it. A deletion asked for at
    # the same moment (accounts.request locks the same row) either lands first,
    # and the row is skipped, or waits until this is done and finds the
    # account retired. Signing in again after keeping an account carries it
    # over then.
    async with conn.transaction():
        # The target and every source, locked in one statement in id order:
        # the order a block takes its two rows in (routes/blocks.py), so the
        # two never wait on each other in a cycle.
        rows = await conn.fetch(
            """
            SELECT id, deletion_requested_at FROM observers
            WHERE id = $2
               OR (created_via = 'passcode'
                   AND email IS NULL
                   AND deleted_at IS NULL
                   AND deletion_requested_at IS NULL
                   -- A banned account stays where it is, hidden, rather than
                   -- bringing its uploads back under a new one (#180).
                   AND banned_at IS NULL
                   AND lower(btrim(display_name)) = $1)
            ORDER BY id
            FOR UPDATE
            """,
            email,
            target,
        )
        if any(r["id"] == target and r["deletion_requested_at"] is not None for r in rows):
            return 0
        ids = [r["id"] for r in rows if r["id"] != target]
        if not ids:
            return 0

        for table, column in _OBSERVER_REFS:
            await conn.execute(
                f"UPDATE {table} SET {column} = $1 WHERE {column} = ANY($2::uuid[])",
                target,
                ids,
            )
        await _carry_blocks(conn, target, ids)
        # Retire rather than delete: the row is evidence of how the data arrived,
        # and soft-deleting keeps this idempotent on a second sign-in.
        await conn.execute(
            "UPDATE observers SET deleted_at = now(), updated_at = now() "
            "WHERE id = ANY($1::uuid[])",
            ids,
        )
    return len(ids)


async def _carry_blocks(conn, target: UUID, ids: list[UUID]) -> None:
    """Blocks made by, and of, the absorbed accounts become the target's (#180).

    `visible_to` compares observer ids, so a block left on a retired id would
    simply lapse, and two people who had blocked each other would see each
    other again. Moved in both directions; a block between the target and an
    absorbed account, or between two absorbed ones, would be the person
    blocking themself and goes; duplicates collapse onto the oldest."""
    everyone = [target, *ids]
    await conn.execute(
        "DELETE FROM observer_blocks WHERE blocker_id = ANY($1::uuid[]) AND blocked_id = ANY($1::uuid[])", everyone)
    for side, other in (("blocker_id", "blocked_id"), ("blocked_id", "blocker_id")):
        await conn.execute(
            f"""DELETE FROM observer_blocks b USING observer_blocks keep
                WHERE b.{side} = ANY($1::uuid[]) AND keep.{side} = ANY($2::uuid[])
                  AND keep.{other} = b.{other}
                  AND (keep.{side} = $3 OR keep.created_at < b.created_at
                       OR (keep.created_at = b.created_at AND keep.id < b.id))""",
            ids, everyone, target)
        await conn.execute(f"UPDATE observer_blocks SET {side} = $1 WHERE {side} = ANY($2::uuid[])", target, ids)
