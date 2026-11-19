"""Data migrations in the launch stack, run against a scratch database.

The shared test database is migrated to head once per session, so a data step
inside a migration (a backfill, a tag) never sees rows that existed before it.
These run alembic step by step on a throwaway database instead.
"""
import os
import subprocess
from uuid import uuid4

import asyncpg
import psycopg
import pytest

from app.config import settings
from tests.conftest import BACKEND_ROOT, _default_db_url_sync


@pytest.fixture
def scratch_db(_migrated):
    name = f"launch_mig_{uuid4().hex[:12]}"
    admin = _default_db_url_sync().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    sync = settings.test_database_url_sync.rsplit("/", 1)[0] + "/" + name
    url = settings.test_database_url.rsplit("/", 1)[0] + "/" + name

    def upgrade(revision):
        env = os.environ.copy()
        env["ALEMBIC_DB_URL"] = sync
        subprocess.run(["uv", "run", "alembic", "upgrade", revision], cwd=BACKEND_ROOT, env=env,
                       check=True, capture_output=True)

    try:
        yield url, upgrade
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.mark.asyncio
async def test_uploads_in_flight_at_deploy_are_screened_not_published(scratch_db):
    """0042: finished uploads stay public; ones still processing become
    `pending`, which their completion screens."""
    url, upgrade = scratch_db
    upgrade("0041_store_reviewer")
    conn = await asyncpg.connect(url)
    try:
        ids = {}
        for state in ("ready", "no_animal", "failed", "queued", "processing", "needs_review", "legacy"):
            ids[state] = uuid4()
            await conn.execute("INSERT INTO captures (id, kind, processing_state, captured_at) "
                               "VALUES ($1, 'photo', $2, now())", ids[state], state)
        # A /sighting upload: its capture says `legacy`, its sighting is in flight.
        legacy_in_flight = uuid4()
        await conn.execute("INSERT INTO captures (id, kind, processing_state, captured_at) "
                           "VALUES ($1, 'photo', 'legacy', now())", legacy_in_flight)
        await conn.execute("INSERT INTO sightings (id, capture_id, captured_at, processing_state) "
                           "VALUES ($1, $2, now(), 'processing')", uuid4(), legacy_in_flight)
    finally:
        await conn.close()
    upgrade("0042_upload_screening")
    conn = await asyncpg.connect(url)
    try:
        got = {r["id"]: r["screening"] for r in await conn.fetch("SELECT id, screening FROM captures")}
    finally:
        await conn.close()
    assert {s: got[i] for s, i in ids.items()} == {
        "ready": "clear", "no_animal": "clear", "failed": "clear", "legacy": "clear",
        "queued": "pending", "processing": "pending", "needs_review": "pending"}
    assert got[legacy_in_flight] == "pending"


@pytest.mark.asyncio
async def test_recipe_tagging_is_metadata_only_and_untagged_vectors_are_refused(scratch_db):
    """0044: existing MiewID vectors read as the baseline recipe without a
    table rewrite; anything else is untagged; a new untagged vector fails."""
    from app.reid_recipe import ACTIVE_RECIPE
    url, upgrade = scratch_db
    upgrade("0043_model_screening")
    conn = await asyncpg.connect(url)
    vec = "[" + ",".join(["0.01"] * 2152) + "]"
    try:
        sid = uuid4()
        await conn.execute("INSERT INTO sightings (id, captured_at) VALUES ($1, now())", sid)
        photos = [uuid4() for _ in range(2)]
        for pid in photos:
            await conn.execute("INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1, $2, 'k')", pid, sid)
        await conn.execute("INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
                           "VALUES ($1, $2, 'miewid-msv3', 2152, $3::vector)", uuid4(), photos[0], vec)
        await conn.execute("INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
                           "VALUES ($1, $2, 'older-model', 2152, $3::vector)", uuid4(), photos[1], vec)
        before = await conn.fetchval("SELECT relfilenode FROM pg_class WHERE relname = 'embeddings'")
    finally:
        await conn.close()
    upgrade("0044_reid_recipe")
    conn = await asyncpg.connect(url)
    try:
        tags = {r["photo_id"]: r["recipe"] for r in await conn.fetch("SELECT photo_id, recipe FROM embeddings")}
        assert tags == {photos[0]: ACTIVE_RECIPE, photos[1]: None}
        # Not rewritten: same storage file as before the column was added.
        assert await conn.fetchval("SELECT relfilenode FROM pg_class WHERE relname = 'embeddings'") == before
        assert await conn.fetchval("SELECT column_default FROM information_schema.columns "
                                   "WHERE table_name = 'embeddings' AND column_name = 'recipe'") is None
        other = uuid4()
        await conn.execute("INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1, $2, 'k')", other, sid)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
                               "VALUES ($1, $2, 'miewid-msv3', 2152, $3::vector)", uuid4(), other, vec)
    finally:
        await conn.close()
