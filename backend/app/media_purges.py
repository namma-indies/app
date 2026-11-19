"""Object-store media owed a delete, and deleting it (#158).

When a voice note added later is deleted, its rows go at once and its audio's
key is recorded in `media_purges` in the same transaction. `purge_later`
then deletes the object after the response, bounded in time, and clears the
record; whatever it cannot delete stays recorded for scripts/purge_media.py.

A key can come back into use: a note's key is named after its recording
(app/routes/voice_notes.py), so the same recording saved again after its note
was deleted writes the same key. Saving and purging therefore take the same
lock on the key (`lock_key`). A save under it takes the key back out of
`media_purges` before writing the object; a purge under it deletes nothing a
live note still points at. Neither can undo the other.
"""
import asyncio
import logging
from uuid import UUID

logger = logging.getLogger(__name__)

# A store that has not answered by then is left to the script.
PURGE_TIMEOUT_S = 30.0
_running: set[asyncio.Task] = set()


async def lock_key(conn, key: str) -> None:
    """Held until the caller's transaction ends."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "media:" + key)


def _owner_id(prefix: str) -> UUID | None:
    try:
        return UUID(prefix.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None


async def _in_use(conn, key: str, kind: str) -> bool:
    if kind == "prefix":
        owner = _owner_id(key)
        return owner is not None and await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM captures WHERE id=$1) OR EXISTS(SELECT 1 FROM sightings WHERE id=$1)",
            owner)
    return await conn.fetchval("SELECT EXISTS(SELECT 1 FROM voice_notes WHERE s3_key=$1)", key)


async def _delete(storage, key: str, kind: str) -> None:
    if kind == "prefix":
        for each in await storage.list_keys(key):
            await storage.delete(each)
    else:
        await storage.delete(key)


async def purge(pool, storage, key: str, *, timeout: float = PURGE_TIMEOUT_S) -> bool:
    """Delete one owed object, or everything under one owed prefix, and its
    record. False if the store refused or did not answer in time; it then
    stays recorded, with the error.

    No transaction is open while the store is asked: the key's lock is taken
    at session level instead, so a save of the same key still waits for it
    (`lock_key` is the same advisory lock), but nothing holds row locks or a
    snapshot across a slow list or delete. The pool resets a connection's
    advisory locks when it is returned, so a cancelled purge cannot leak one."""
    lock = "media:" + key
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", lock)
        try:
            kind = await conn.fetchval("SELECT kind FROM media_purges WHERE s3_key=$1", key)
            if kind is None:
                return True                               # done already, or taken back by a save
            if await _in_use(conn, key, kind):
                await conn.execute("DELETE FROM media_purges WHERE s3_key=$1", key)
                return True                               # something live points at it again
            try:
                await asyncio.wait_for(_delete(storage, key, kind), timeout)
            except Exception as error:
                # Logged by key and type, never by content.
                logger.warning("media left for purging: key=%s (%s)", key, type(error).__name__)
                await conn.execute("UPDATE media_purges SET attempts=attempts+1, last_error=$2 WHERE s3_key=$1",
                                   key, type(error).__name__)
                return False
            await conn.execute("DELETE FROM media_purges WHERE s3_key=$1", key)
            return True
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock)


_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    """At most `media_purge_concurrency` background purges at once, per loop."""
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or _gate[0] is not loop:
        from app.config import settings
        _gate = (loop, asyncio.Semaphore(settings.media_purge_concurrency))
    return _gate[1]


async def _purge_gated(pool, storage, key: str) -> bool:
    async with _semaphore():
        return await purge(pool, storage, key)


def purge_later(pool, storage, key: str) -> None:
    """Purge after the response has gone: nobody waits on the store. Queued
    behind `media_purge_concurrency`, so an account erasing thousands of
    uploads cannot take the whole pool."""
    task = asyncio.get_running_loop().create_task(_purge_gated(pool, storage, key))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def settled() -> None:
    """Wait for purges started here to finish (for tests)."""
    while _running:
        await asyncio.gather(*list(_running), return_exceptions=True)
