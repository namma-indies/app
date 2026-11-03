"""Deleting an account (#179).

LIFECYCLE
---------
    active --request--> pending (grace) --purge_after--> purging --> deleted
                           |
                           +--"Keep my account" (signed in)--> active

**Request** (`request`): from the app, signed in, or from the web page with a
link mailed to the address (routes/account.py). From that moment everything
the person logged is off every shared surface (`aggregates.owner_active`), and
their session reaches only /account (`auth.deps.require_observer`). Nothing is
deleted yet: the grace period (`account_deletion_grace_days`, Q1) is for a tap
they did not mean, or a phone that was not theirs to tap.

**Cancel** (`cancel`): while still scheduled, signed in. Everything comes back
as it was, because nothing was changed but the flag.

**Purge** (`purge_one`, run by `consumer` in the API process): every upload is
erased exactly as "Delete photo" erases it (app/erasure.py), one transaction
each, so a purge stopped halfway resumes where it stopped. Then what is left
of the person goes, and their row becomes a tombstone.

THE TOMBSTONE (U3)
------------------
The `observers` row is kept, with every personal field emptied
(`OBSERVER_COLUMNS`): the id remains, because other people's records name it --
a verdict they gave on someone else's pair, a moderator's decision -- and a
bare id with nothing behind it identifies no one. `OBSERVER_REFERENCES` lists
every foreign key into `observers` and what happens to its rows; the guard
test fails when a migration adds one that is not listed.

If it is decided that even the id must go, the switch is one migration: give
each `kept` key in `OBSERVER_REFERENCES` ON DELETE SET NULL, and replace the
scrub in `_finish` with `DELETE FROM observers WHERE id=$1`.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from uuid import UUID

from app.config import settings
from app.erasure import Erased, erase, write_ledger

logger = logging.getLogger(__name__)

# Every foreign key into `observers`. "erased": the rows go with the account.
# "kept": the row is someone else's record, or a moderator's decision, and
# keeps pointing at the anonymous tombstone.
OBSERVER_REFERENCES: dict[tuple[str, str], str] = {
    ("captures", "observer_id"): "erased",
    ("sightings", "observer_id"): "erased",
    ("voice_notes", "observer_id"): "erased",
    ("login_tokens", "observer_id"): "erased",
    ("sighting_reports", "reporter_id"): "erased",
    # Name claims they made go (Q4); a name the community adopted would stay,
    # unattributed -- nothing adopts names yet.
    ("individual_names", "proposed_by"): "erased",
    ("individual_names", "resolved_by"): "kept",
    ("individuals", "created_by_observer"): "kept",
    ("individuals", "named_by"): "kept",
    ("match_proposals", "resolved_by"): "kept",      # their verdicts on others' pairs (Q5)
    ("confirmations", "observer_id"): "kept",
    ("sightings", "reviewed_by"): "kept",            # as a moderator
    ("sightings", "animal_reviewed_by"): "kept",
    ("observers", "created_by_observer"): "kept",
    ("social_picks", "picked_by"): "kept",
    ("detail_suggestions", "decided_by"): "kept",
    ("photo_descriptions", "decided_by"): "kept",
    # Their blocks, both ways, go with them (#180).
    ("observer_blocks", "blocker_id"): "erased",
    ("observer_blocks", "blocked_id"): "erased",
    ("observers", "banned_by"): "kept",              # as a moderator
}

# Every column of `observers`: what the tombstone keeps, and what it empties.
OBSERVER_COLUMNS: dict[str, str] = {
    "id": "kept",
    "created_via": "kept",
    "created_at": "kept",
    "updated_at": "kept",
    "deleted_at": "kept",
    "deletion_requested_at": "kept",
    "created_by_observer": "kept",
    "phone_hash": "emptied",
    "contact_enc": "emptied",
    "display_name": "emptied",
    "trust_tier": "emptied",
    "home_geog": "emptied",
    "home_radius_m": "emptied",
    "email": "emptied",
    "x_handle": "emptied",
    "note_language": "emptied",
    "social_consent": "false",
    # A ban is kept on the tombstone; the address it was for lives on only as
    # a keyed hash in banned_emails (#180).
    "banned_at": "kept",
    "banned_by": "kept",
    "ban_reason": "emptied",
    # The demo marker enforces one identity, even on an erased tombstone.
    "store_reviewer": "kept",
}

LEASE = timedelta(minutes=15)
RETRY_AFTER = timedelta(minutes=10)


def grace() -> timedelta:
    return timedelta(days=settings.account_deletion_grace_days)


async def state(conn, observer_id: UUID) -> dict:
    """What /account shows: whether deletion is scheduled, when, and how much
    would go."""
    row = await conn.fetchrow(
        """SELECT o.email IS NOT NULL AS has_email, o.deletion_requested_at, o.banned_at IS NOT NULL AS suspended,
                  d.purge_after, d.status
           FROM observers o LEFT JOIN account_deletions d ON d.observer_id = o.id
           WHERE o.id = $1""", observer_id)
    pending = row["deletion_requested_at"] is not None
    counts = await conn.fetchrow(
        """SELECT (SELECT count(*) FROM captures WHERE observer_id=$1) AS uploads,
                  (SELECT count(*) FROM sightings WHERE observer_id=$1) AS sightings,
                  (SELECT count(*) FROM voice_notes WHERE observer_id=$1) AS voice_notes""", observer_id)
    return {
        "status": "pending_deletion" if pending else "active",
        "purge_after": row["purge_after"].isoformat() if pending and row["purge_after"] else None,
        # A purge already under way cannot be stopped; only a scheduled one.
        "can_cancel": pending and row["status"] == "scheduled",
        "has_email": row["has_email"],
        "suspended": row["suspended"],
        "grace_days": settings.account_deletion_grace_days,
        "counts": dict(counts),
    }


async def request(conn, observer_id: UUID, via: str) -> tuple[datetime, bool] | None:
    """Schedule the account for deletion. (when it goes, newly scheduled), or
    None if there is no such account. Asking again changes nothing. Caller
    holds a transaction."""
    row = await conn.fetchrow(
        "SELECT deleted_at, deletion_requested_at FROM observers WHERE id=$1 FOR UPDATE", observer_id)
    if row is None or row["deleted_at"] is not None:
        return None
    if row["deletion_requested_at"] is not None:
        current = await conn.fetchval(
            "SELECT purge_after FROM account_deletions WHERE observer_id=$1 AND status IN ('scheduled','purging')",
            observer_id)
        if current is not None:
            return current, False
    when = await conn.fetchval(
        """INSERT INTO account_deletions (observer_id, via, purge_after)
           VALUES ($1, $2, now() + $3::interval)
           ON CONFLICT (observer_id) DO UPDATE SET via=EXCLUDED.via, requested_at=now(),
               purge_after=EXCLUDED.purge_after, status='scheduled', attempts=0, last_error=NULL,
               lease_until=NULL, started_at=NULL, completed_at=NULL, cancelled_at=NULL
           RETURNING purge_after""", observer_id, via, grace())
    await conn.execute(
        "UPDATE observers SET deletion_requested_at=now(), updated_at=now() WHERE id=$1", observer_id)
    # Any other deletion link still in a mailbox has nothing left to do.
    await conn.execute(
        "DELETE FROM login_tokens WHERE observer_id=$1 AND purpose='delete' AND used_at IS NULL", observer_id)
    logger.info("account deletion scheduled: observer=%s via=%s purge_after=%s", observer_id, via, when)
    return when, True


async def cancel(conn, observer_id: UUID) -> bool:
    """Keep the account. False once the purge has started: by then some of it
    is gone, and pretending otherwise would be worse than saying so."""
    kept = await conn.fetchval(
        """UPDATE account_deletions SET status='cancelled', cancelled_at=now()
           WHERE observer_id=$1 AND status='scheduled' RETURNING observer_id""", observer_id)
    if kept is None:
        return False
    await conn.execute(
        "UPDATE observers SET deletion_requested_at=NULL, updated_at=now() WHERE id=$1", observer_id)
    logger.info("account deletion cancelled: observer=%s", observer_id)
    return True


async def _claim(pool) -> UUID | None:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """UPDATE account_deletions SET status='purging', started_at=COALESCE(started_at, now()),
                   attempts=attempts+1, lease_until=now() + $1::interval
               WHERE observer_id = (
                   SELECT observer_id FROM account_deletions
                   WHERE status IN ('scheduled', 'purging') AND purge_after <= now()
                     AND (lease_until IS NULL OR lease_until < now())
                   ORDER BY purge_after FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING observer_id""", LEASE)


async def _each(pool, sql: str, observer_id: UUID):
    """The next row `sql` finds for this observer, until there are none."""
    last = None
    while True:
        async with pool.acquire() as conn:
            current = await conn.fetchval(sql, observer_id)
        if current is None:
            return
        if current == last:
            raise RuntimeError(f"erasure made no progress on {current}")
        last = current
        yield current


async def erase_account(pool, storage, observer_id: UUID) -> None:
    """Erase everything an observer logged, then what is left of them.
    Idempotent: run it again after a crash and it carries on."""
    from app.media_purges import purge_later
    async for capture_id in _each(pool, "SELECT id FROM captures WHERE observer_id=$1 ORDER BY id LIMIT 1",
                                  observer_id):
        await erase(pool, storage, "capture", capture_id, reason="account_deleted", ledger=False)
    # Every sighting has a capture (0015); any that does not is erased alone.
    async for sighting_id in _each(pool, "SELECT id FROM sightings WHERE observer_id=$1 ORDER BY id LIMIT 1",
                                   observer_id):
        await erase(pool, storage, "sighting", sighting_id, reason="account_deleted", ledger=False)
    keys = await _finish(pool, storage, observer_id)
    for key in keys:
        purge_later(pool, storage, key)


async def _finish(pool, storage, observer_id: UUID) -> list[str]:
    async with pool.acquire() as conn, conn.transaction():
        # Notes on someone else's upload cannot be made today; if one ever is,
        # it is still theirs.
        keys = [r["s3_key"] for r in await conn.fetch(
            "DELETE FROM voice_notes WHERE observer_id=$1 RETURNING s3_key", observer_id)]
        for key in keys:
            await conn.execute(
                "INSERT INTO media_purges (s3_key, reason, kind) VALUES ($1,'account_deleted','key') "
                "ON CONFLICT DO NOTHING", key)
        await conn.execute("DELETE FROM sighting_reports WHERE reporter_id=$1", observer_id)
        await conn.execute("DELETE FROM observer_blocks WHERE blocker_id=$1 OR blocked_id=$1", observer_id)
        await conn.execute("DELETE FROM login_tokens WHERE observer_id=$1", observer_id)
        await conn.execute(
            "DELETE FROM individual_names WHERE proposed_by=$1 AND status IN ('proposed','rejected')", observer_id)
        emptied = ", ".join(f"{c} = NULL" for c, how in OBSERVER_COLUMNS.items() if how == "emptied")
        await conn.execute(
            f"UPDATE observers SET {emptied}, social_consent = false, deleted_at = now(), updated_at = now() "
            "WHERE id = $1", observer_id)
        await conn.execute(
            """UPDATE account_deletions SET status='done', completed_at=now(), lease_until=NULL, last_error=NULL
               WHERE observer_id=$1""", observer_id)
        if settings.erasure_ledger_enabled:
            await write_ledger(storage, Erased("account", observer_id))
    logger.info("account erased: observer=%s", observer_id)
    return keys


async def purge_one(pool, storage) -> UUID | None:
    """Erase one account whose grace period is over. None when none is due."""
    observer_id = await _claim(pool)
    if observer_id is None:
        return None
    try:
        await erase_account(pool, storage, observer_id)
    except Exception as exc:
        # Logged by type: what failed could quote someone's data.
        logger.warning("account purge failed: observer=%s (%s)", observer_id, type(exc).__name__)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE account_deletions SET last_error=$2, lease_until=now() + $3::interval WHERE observer_id=$1",
                observer_id, type(exc).__name__, RETRY_AFTER)
        return None
    return observer_id


async def consumer(pool, storage) -> None:
    """Purge accounts as their grace periods end, for as long as the API runs."""
    while True:
        try:
            while await purge_one(pool, storage) is not None:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("account purge loop failed")
        await asyncio.sleep(settings.account_purge_interval_s)
