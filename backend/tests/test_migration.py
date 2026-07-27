import asyncpg, pytest

TABLES = {"observers","sightings","photos","embeddings","individuals",
          "match_proposals","confirmations","clinical_records","areas","jobs"}

@pytest.mark.asyncio
async def test_all_tables_exist(migrated_db):
    rows = await migrated_db.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    assert TABLES.issubset({r['tablename'] for r in rows})

@pytest.mark.asyncio
async def test_public_read_has_no_grant_on_sightings(migrated_db):
    has = await migrated_db.fetchval("SELECT has_table_privilege('public_read','sightings','SELECT')")
    assert has is False

@pytest.mark.asyncio
async def test_sightings_geog_is_gist_indexed(migrated_db):
    idx = await migrated_db.fetch("SELECT indexdef FROM pg_indexes WHERE tablename='sightings'")
    assert any('gist' in r['indexdef'].lower() and 'geog' in r['indexdef'].lower() for r in idx)

@pytest.mark.asyncio
async def test_sightings_individual_id_nullable(migrated_db):
    nn = await migrated_db.fetchval(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='sightings' AND column_name='individual_id'")
    assert nn == 'YES'

@pytest.mark.asyncio
async def test_embeddings_unique_photo_model(migrated_db):
    cnt = await migrated_db.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname LIKE '%photo_id%model%' OR conname LIKE '%embeddings%'")
    # a UNIQUE(photo_id, model) constraint exists
    con = await migrated_db.fetch(
        "SELECT conname, contype FROM pg_constraint c JOIN pg_class t ON c.conrelid=t.oid "
        "WHERE t.relname='embeddings' AND contype='u'")
    assert len(con) >= 1


@pytest.mark.asyncio
async def test_observers_has_unique_email(migrated_db):
    col = await migrated_db.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='observers' AND column_name='email'")
    assert col == 'text'
    idx = await migrated_db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename='observers'")
    assert any('email' in r['indexdef'] and 'UNIQUE' in r['indexdef'].upper() for r in idx)


@pytest.mark.asyncio
async def test_login_tokens_table_shape(migrated_db):
    cols = {r['column_name']: r['is_nullable'] for r in await migrated_db.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name='login_tokens'")}
    assert cols == {
        'token_hash': 'NO', 'observer_id': 'NO', 'expires_at': 'NO',
        'used_at': 'YES', 'created_at': 'NO',
    }


@pytest.mark.asyncio
async def test_login_tokens_cascade_on_observer_delete(migrated_db):
    from app.ids import uuid7
    oid = uuid7()
    await migrated_db.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'X','test')", oid)
    await migrated_db.execute(
        "INSERT INTO login_tokens (token_hash, observer_id, expires_at) "
        "VALUES ('deadbeef', $1, now() + interval '1 hour')", oid)
    await migrated_db.execute("DELETE FROM observers WHERE id = $1", oid)
    assert await migrated_db.fetchval("SELECT count(*) FROM login_tokens") == 0
