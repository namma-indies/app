import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.auth import logintoken
from app.auth.magiclink import create_observer
from app.config import Settings, settings


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["login", "delete"])
@pytest.mark.parametrize("used", [False, True])
async def test_retention_is_seven_days_after_expiry_for_both_purposes(migrated_db, purpose, used, monkeypatch):
    conn = migrated_db
    monkeypatch.setattr(settings, "login_token_retention_days", 7)
    # PostgreSQL's now() stays fixed inside this transaction, so the exact
    # retention boundary and one second on either side are deterministic.
    async with conn.transaction():
        oid = await create_observer(conn, display_name="Test", created_via="test")
        older = await logintoken.issue(conn, oid, ttl_s=-7 * 86400 - 1, purpose=purpose)
        boundary = await logintoken.issue(conn, oid, ttl_s=-7 * 86400, purpose=purpose)
        recent = await logintoken.issue(conn, oid, ttl_s=-7 * 86400 + 1, purpose=purpose)
        expired = await logintoken.issue(conn, oid, ttl_s=-1, purpose=purpose)
        active = await logintoken.issue(conn, oid, purpose=purpose)
        if used:
            await conn.execute(
                "UPDATE login_tokens SET used_at = expires_at - interval '30 minutes' WHERE observer_id=$1", oid)

        assert await logintoken.purge_expired(conn) == 2
        hashes = {r["token_hash"] for r in await conn.fetch("SELECT token_hash FROM login_tokens")}
        assert hashes == {logintoken._hash(raw) for raw in (recent, expired, active)}
        assert logintoken._hash(older) not in hashes
        assert logintoken._hash(boundary) not in hashes
        assert await logintoken.purge_expired(conn) == 0
        assert await conn.fetchval("SELECT count(*) FROM observers WHERE id=$1", oid) == 1
        assert await logintoken.consume(conn, active, purpose=purpose) == (None if used else oid)
        assert await logintoken.consume(conn, active, purpose=purpose) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 2, 14])
async def test_retention_is_configurable_without_deleting_unexpired_links(migrated_db, monkeypatch, days):
    conn = migrated_db
    monkeypatch.setattr(settings, "login_token_retention_days", days)
    async with conn.transaction():
        oid = await create_observer(conn, display_name="Test", created_via="test")
        await logintoken.issue(conn, oid, ttl_s=-days * 86400, purpose="delete")
        recent = await logintoken.issue(conn, oid, ttl_s=-days * 86400 + 1, purpose="delete")
        active = await logintoken.issue(conn, oid)
        assert await logintoken.purge_expired(conn) == 1
        hashes = {r["token_hash"] for r in await conn.fetch("SELECT token_hash FROM login_tokens")}
        assert hashes == {logintoken._hash(recent), logintoken._hash(active)}
        assert await logintoken.consume(conn, active) == oid


@pytest.mark.asyncio
async def test_restricted_application_role_can_manage_token_history(migrated_db, monkeypatch):
    conn = migrated_db
    monkeypatch.setattr(settings, "login_token_retention_days", 7)
    oid = await create_observer(conn, display_name="Test", created_via="test")
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE app_rw")
        active = await logintoken.issue(conn, oid)
        deleted = await logintoken.issue(conn, oid, purpose="delete")
        await logintoken.issue(conn, oid, ttl_s=-8 * 86400)
        assert await logintoken.consume(conn, deleted, purpose="delete") == oid
        await conn.execute(
            "UPDATE login_tokens SET expires_at=now() - interval '8 days' WHERE token_hash=$1",
            logintoken._hash(deleted))
        assert await logintoken.purge_expired(conn) == 2
        assert await logintoken.peek(conn, active, purpose="login") == oid
        assert await logintoken.consume(conn, active) == oid
        assert await conn.fetchval("SELECT count(*) FROM login_tokens") == 1


def test_housekeeping_defaults():
    config = Settings(_env_file=None)
    assert config.login_token_retention_days == 7
    assert config.login_token_cleanup_interval_s == 3600


@pytest.mark.parametrize("values", [{"login_token_retention_days": -1}, {"login_token_cleanup_interval_s": 0}])
def test_invalid_housekeeping_settings_are_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


class Pool:
    def __init__(self):
        self.conn = object()
        self.acquired = False
        self.releases = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquired = True
        try:
            yield self.conn
        finally:
            self.acquired = False
            self.releases += 1


@pytest.mark.asyncio
async def test_worker_runs_immediately_releases_connections_and_retries(monkeypatch, caplog):
    pool = Pool()
    sweep = AsyncMock(side_effect=[RuntimeError("private row detail"), 3])
    monkeypatch.setattr(logintoken, "purge_expired", sweep)
    monkeypatch.setattr(settings, "login_token_cleanup_interval_s", 17)
    waits = []

    async def pause(delay):
        assert not pool.acquired
        waits.append(delay)
        if len(waits) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(logintoken.asyncio, "sleep", pause)
    with caplog.at_level(logging.INFO, logger=logintoken.__name__):
        with pytest.raises(asyncio.CancelledError):
            await logintoken.consumer(pool)
    assert sweep.await_count == 2
    assert sweep.await_args.args == (pool.conn,)
    assert waits == [17, 17]
    assert pool.releases == 2
    assert "RuntimeError" in caplog.text
    assert "count=3" in caplog.text
    assert "private row detail" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_during_a_sweep_is_not_retried(monkeypatch):
    pool = Pool()
    sweep = AsyncMock(side_effect=asyncio.CancelledError)
    sleep = AsyncMock()
    monkeypatch.setattr(logintoken, "purge_expired", sweep)
    monkeypatch.setattr(logintoken.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await logintoken.consumer(pool)
    assert pool.releases == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_starts_housekeeping_and_stops_it_before_closing_the_pool(monkeypatch):
    from app import accounts, main

    pool = SimpleNamespace(close=AsyncMock())
    storage = SimpleNamespace(ensure_bucket=AsyncMock())
    monkeypatch.setattr(main.asyncpg, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(main, "get_storage", lambda: storage)
    monkeypatch.setattr(settings, "media_jobs_enabled", False)
    monkeypatch.setattr(main, "suggestions_running", lambda: False)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def housekeeping(given_pool):
        assert given_pool is pool
        started.set()
        try:
            await asyncio.Future()
        finally:
            pool.close.assert_not_awaited()
            stopped.set()

    async def account_purger(*args):
        await asyncio.Future()

    monkeypatch.setattr(logintoken, "consumer", housekeeping)
    monkeypatch.setattr(accounts, "consumer", account_purger)
    app = SimpleNamespace(state=SimpleNamespace())
    async with main.lifespan(app):
        await asyncio.wait_for(started.wait(), timeout=5)
    assert stopped.is_set()
    pool.close.assert_awaited_once()
