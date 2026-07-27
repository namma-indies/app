import pytest

from app.auth.email_login import get_or_create_observer_by_email


@pytest.mark.asyncio
async def test_creates_observer_on_first_sight(migrated_db):
    oid = await get_or_create_observer_by_email(
        migrated_db, email="akash@dognosis.tech", display_name="Akash")
    row = await migrated_db.fetchrow(
        "SELECT email, display_name, created_via FROM observers WHERE id = $1", oid)
    assert row["email"] == "akash@dognosis.tech"
    assert row["display_name"] == "Akash"
    assert row["created_via"] == "email"


@pytest.mark.asyncio
async def test_same_email_resolves_to_same_observer(migrated_db):
    """The whole point: a returning person is one identity, not many."""
    first = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    second = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    assert first == second
    assert await migrated_db.fetchval("SELECT count(*) FROM observers") == 1


@pytest.mark.asyncio
async def test_existing_display_name_is_not_overwritten(migrated_db):
    oid = await get_or_create_observer_by_email(
        migrated_db, email="a@dognosis.tech", display_name="Akash")
    await get_or_create_observer_by_email(
        migrated_db, email="a@dognosis.tech", display_name="Someone Else")
    name = await migrated_db.fetchval(
        "SELECT display_name FROM observers WHERE id = $1", oid)
    assert name == "Akash"


@pytest.mark.asyncio
async def test_falls_back_to_local_part_when_no_display_name(migrated_db):
    oid = await get_or_create_observer_by_email(migrated_db, email="priya@dognosis.tech")
    name = await migrated_db.fetchval(
        "SELECT display_name FROM observers WHERE id = $1", oid)
    assert name == "priya"


@pytest.mark.asyncio
async def test_passcode_observers_are_untouched(migrated_db):
    """A NULL-email passcode observer must not be matched or clobbered."""
    from app.auth.magiclink import create_observer
    pc = await create_observer(migrated_db, display_name="Field Tester", created_via="passcode")
    oid = await get_or_create_observer_by_email(migrated_db, email="a@dognosis.tech")
    assert oid != pc
    assert await migrated_db.fetchval("SELECT count(*) FROM observers") == 2
