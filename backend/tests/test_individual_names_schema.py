"""The names log, built before there are any names to lose.

Naming today is three columns on `individuals` -- a single, destructive,
last-writer-wins name, which is exactly the tourist-overwrites-resident
failure issue #4 exists to prevent. This table is the append-only half of the
fix. Nothing reads it yet; the migration is here because it is free while no
names exist and expensive afterwards.
"""

import pytest

from app.ids import uuid7


async def _individual(conn):
    iid = uuid7()
    await conn.execute("INSERT INTO individuals (id) VALUES ($1)", iid)
    return iid


async def test_a_name_can_be_proposed_by_nobody(migrated_db):
    """Model suggestions and WhatsApp intake have no account behind them.
    NOT NULL here would force a fake observer row per name, which is worse
    than an honest null."""
    iid = await _individual(migrated_db)
    await migrated_db.execute(
        "INSERT INTO individual_names (id, individual_id, name) VALUES ($1, $2, 'Kaju')",
        uuid7(), iid,
    )
    row = await migrated_db.fetchrow("SELECT status, proposed_by FROM individual_names")
    assert row["status"] == "proposed"
    assert row["proposed_by"] is None


async def test_many_names_may_be_proposed_for_one_dog(migrated_db):
    """Kaju to the woman at the gate, Blackie to the shop two doors down.
    Nothing here decides between them -- that is issue #55."""
    iid = await _individual(migrated_db)
    for name in ("Kaju", "Blackie", "Kalu"):
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name) VALUES ($1, $2, $3)",
            uuid7(), iid, name,
        )
    assert await migrated_db.fetchval("SELECT count(*) FROM individual_names") == 3


async def test_only_one_name_can_be_active_at_a_time(migrated_db):
    """`individuals.name` caches the active row. The database refuses the
    inconsistent state rather than trusting code that does not exist yet."""
    iid = await _individual(migrated_db)
    await migrated_db.execute(
        "INSERT INTO individual_names (id, individual_id, name, status) "
        "VALUES ($1, $2, 'Kaju', 'active')",
        uuid7(), iid,
    )
    with pytest.raises(Exception):
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name, status) "
            "VALUES ($1, $2, 'Bruno', 'active')",
            uuid7(), iid,
        )


async def test_two_dogs_may_each_have_an_active_name(migrated_db):
    for _ in range(2):
        iid = await _individual(migrated_db)
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name, status) "
            "VALUES ($1, $2, 'Kaju', 'active')",
            uuid7(), iid,
        )
    assert await migrated_db.fetchval(
        "SELECT count(*) FROM individual_names WHERE status = 'active'"
    ) == 2


async def test_status_is_constrained(migrated_db):
    iid = await _individual(migrated_db)
    with pytest.raises(Exception):
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name, status) "
            "VALUES ($1, $2, 'Kaju', 'canonical')",
            uuid7(), iid,
        )
