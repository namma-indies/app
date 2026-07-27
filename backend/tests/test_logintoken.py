import pytest

from app.auth.logintoken import consume, issue


@pytest.mark.asyncio
async def test_issued_token_consumes_to_its_observer(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid)
    assert await consume(migrated_db, raw) == oid


@pytest.mark.asyncio
async def test_token_is_single_use(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid)
    assert await consume(migrated_db, raw) == oid
    assert await consume(migrated_db, raw) is None


@pytest.mark.asyncio
async def test_expired_token_is_rejected(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid, ttl_s=-1)
    assert await consume(migrated_db, raw) is None


@pytest.mark.asyncio
async def test_garbage_token_is_rejected(migrated_db):
    assert await consume(migrated_db, "not-a-real-token") is None
    assert await consume(migrated_db, "") is None


@pytest.mark.asyncio
async def test_raw_token_is_never_stored(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    raw = await issue(migrated_db, oid)
    stored = await migrated_db.fetchval("SELECT token_hash FROM login_tokens")
    assert stored != raw
    assert len(stored) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_tokens_are_distinct(migrated_db):
    from app.auth.magiclink import create_observer
    oid = await create_observer(migrated_db, display_name="A", created_via="email")
    assert await issue(migrated_db, oid) != await issue(migrated_db, oid)
