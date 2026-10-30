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

logger = logging.getLogger(__name__)

# A store that has not answered by then is left to the script.
PURGE_TIMEOUT_S = 30.0
_running: set[asyncio.Task] = set()


async def lock_key(conn, key: str) -> None:
    """Held until the caller's transaction ends."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "media:" + key)


async def purge(pool, storage, key: str, *, timeout: float = PURGE_TIMEOUT_S) -> bool:
    """Delete one owed object and its record. False if the store refused or
    did not answer in time; the key then stays recorded, with the error."""
    async with pool.acquire() as conn, conn.transaction():
        await lock_key(conn, key)
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM media_purges WHERE s3_key=$1)", key):
            return True                                   # done already, or taken back by a save
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM voice_notes WHERE s3_key=$1)", key):
            await conn.execute("DELETE FROM media_purges WHERE s3_key=$1", key)
            return True                                   # a live note points at it again
        try:
            await asyncio.wait_for(storage.delete(key), timeout)
        except Exception as error:
            # Logged by key and type, never by content.
            logger.warning("media left for purging: key=%s (%s)", key, type(error).__name__)
            await conn.execute("UPDATE media_purges SET attempts=attempts+1, last_error=$2 WHERE s3_key=$1",
                               key, type(error).__name__)
            return False
        await conn.execute("DELETE FROM media_purges WHERE s3_key=$1", key)
        return True


def purge_later(pool, storage, key: str) -> None:
    """Purge after the response has gone: nobody waits on the store."""
    task = asyncio.get_running_loop().create_task(purge(pool, storage, key))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def settled() -> None:
    """Wait for purges started here to finish (for tests)."""
    while _running:
        await asyncio.gather(*list(_running), return_exceptions=True)
