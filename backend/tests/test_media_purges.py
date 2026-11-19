"""Purging owed media from the store without starving the pool (#196 review).

An erased account fires one purge per upload. Each used to hold a pool
connection *and an open transaction* for as long as the store took to list and
delete -- so a prolific account could take every connection the API had.
"""
import asyncio

import asyncpg
import pytest

from app.config import settings
from app.media_purges import purge, purge_later, settled


class SlowStore:
    """A store that takes a moment per call and remembers how many it served at once."""

    def __init__(self, pool, delay=0.05):
        self.pool, self.delay = pool, delay
        self.now = self.most = 0
        self.idle_in_tx: list[int] = []
        self.deleted: list[str] = []

    async def _visit(self):
        self.now += 1
        self.most = max(self.most, self.now)
        async with self.pool.acquire() as c:
            self.idle_in_tx.append(await c.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND state LIKE 'idle in transaction%'"))
        await asyncio.sleep(self.delay)
        self.now -= 1

    async def list_keys(self, prefix):
        await self._visit()
        return [prefix + "a.webp", prefix + "b.webp"]

    async def delete(self, key):
        await self._visit()
        self.deleted.append(key)


@pytest.mark.asyncio
async def test_a_purge_holds_no_transaction_while_the_store_works(migrated_db):
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=4)
    try:
        await migrated_db.execute(
            "INSERT INTO media_purges (s3_key, reason, kind) VALUES ('captures/x/','test','prefix')")
        store = SlowStore(pool)
        assert await purge(pool, store, "captures/x/")
        assert store.deleted == ["captures/x/a.webp", "captures/x/b.webp"]
        assert store.idle_in_tx and all(n == 0 for n in store.idle_in_tx)
        assert await migrated_db.fetchval("SELECT count(*) FROM media_purges") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_background_purges_are_bounded_and_leave_the_pool_usable(migrated_db, monkeypatch):
    monkeypatch.setattr(settings, "media_purge_concurrency", 2)
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=4)
    try:
        keys = [f"captures/{i:032x}/" for i in range(12)]
        for key in keys:
            await migrated_db.execute(
                "INSERT INTO media_purges (s3_key, reason, kind) VALUES ($1,'test','prefix')", key)
        store = SlowStore(pool)
        for key in keys:
            purge_later(pool, store, key)
        # While they run, the API can still get a connection quickly.
        await asyncio.sleep(0.02)
        async with asyncio.timeout(1):
            async with pool.acquire() as c:
                assert await c.fetchval("SELECT 1") == 1
        await settled()
        assert store.most <= 2
        assert await migrated_db.fetchval("SELECT count(*) FROM media_purges") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_save_of_the_same_key_waits_for_a_purge_in_progress(migrated_db):
    """The key lock still covers the store call, though no transaction does."""
    from app.media_purges import lock_key
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=4)
    try:
        await migrated_db.execute("INSERT INTO media_purges (s3_key, reason) VALUES ('notes/k.m4a','test')")
        started, release = asyncio.Event(), asyncio.Event()

        class Held:
            async def delete(self, key):
                started.set()
                await release.wait()

        task = asyncio.create_task(purge(pool, Held(), "notes/k.m4a"))
        await started.wait()
        async with pool.acquire() as c, c.transaction():
            await c.execute("SET LOCAL lock_timeout = '200ms'")
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await lock_key(c, "notes/k.m4a")
        release.set()
        assert await task
        async with pool.acquire() as c, c.transaction():
            await lock_key(c, "notes/k.m4a")                     # free again
    finally:
        await pool.close()
