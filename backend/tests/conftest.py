import os
import subprocess
from pathlib import Path

import asyncpg
import psycopg
import pytest
import pytest_asyncio

from app.config import settings

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _default_db_url_sync() -> str:
    """The sync URL to the always-present `postgres` maintenance database,
    derived from the configured test database's sync URL."""
    url = settings.test_database_url_sync
    prefix, _, _rest = url.rpartition("/")
    return f"{prefix}/postgres"


def _ensure_test_database() -> None:
    admin_url = _default_db_url_sync().replace("postgresql+psycopg://", "postgresql://")
    dbname = settings.test_database_url_sync.rsplit("/", 1)[-1]
    with psycopg.connect(admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f'CREATE DATABASE "{dbname}"')


def _run_migrations() -> None:
    env = os.environ.copy()
    env["ALEMBIC_DB_URL"] = settings.test_database_url_sync
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
    )


@pytest.fixture(scope="session")
def _migrated():
    """Ensures the test DB exists and is migrated. NOT autouse: only tests
    that actually need Postgres should request this (directly, or
    transitively via `migrated_db` / `app_client` / `authed_client`), so
    pure-unit test files can run without Postgres available."""
    _ensure_test_database()
    _run_migrations()
    yield


@pytest_asyncio.fixture
async def migrated_db(_migrated):
    conn = await asyncpg.connect(settings.test_database_url)
    try:
        await conn.execute(
            "TRUNCATE observers, sightings, photos, embeddings, individuals, "
            "match_proposals, confirmations, clinical_records, login_tokens "
            "RESTART IDENTITY CASCADE"
        )
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_client(_migrated):
    from app import db

    db.set_dsn_override(settings.test_database_url)
    from app.main import app

    app.state.pool = await asyncpg.create_pool(settings.test_database_url)

    from app.deps import get_storage

    await get_storage().ensure_bucket()

    async with app.state.pool.acquire() as c:
        await c.execute(
            "TRUNCATE observers, sightings, photos, embeddings, individuals, "
            "match_proposals, confirmations, clinical_records, login_tokens "
            "RESTART IDENTITY CASCADE"
        )

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await app.state.pool.close()
    db.set_dsn_override(None)


@pytest_asyncio.fixture
async def authed_client(app_client):
    from app.ids import uuid7
    from app.security import issue_session

    oid = uuid7()
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,$2,'test')",
            oid,
            "Tester",
        )
    app_client.cookies.set("session", issue_session(oid))
    yield app_client, oid
