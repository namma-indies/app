import asyncio
import json

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException
from botocore.exceptions import ClientError

from app.abuse import snapshot
from app.auth.email_login import get_or_create_observer_by_email
from app.config import settings
from app.email.delivery import deliver, reserve
from app.ids import uuid7
from app.security import issue_session


async def test_both_upload_doors_share_account_budget_before_body_is_read(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "rl_upload_hour_times", 2)
    assert (await client.post("/sighting")).status_code == 422
    assert (await client.post("/capture")).status_code == 422
    read = False
    async def body():
        nonlocal read
        read = True
        yield b"untrusted upload bytes"
    response = await client.post("/sighting", content=body(), headers={"Origin": "capacitor://localhost"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert not read
    assert "retry-after" in response.headers["access-control-expose-headers"].lower()
    assert "queued photos are safe" in response.json()["detail"]
    other = uuid7()
    async with client._transport.app.state.pool.acquire() as conn:
        await conn.execute("INSERT INTO observers (id) VALUES ($1)", other)
    assert (await client.post("/capture", headers={"Cookie": f"session={issue_session(other)}"})).status_code == 422


async def test_concurrent_uploads_cannot_multiply_the_budget(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "rl_upload_hour_times", 3)
    responses = await asyncio.gather(*(client.post("/sighting") for _ in range(16)))
    assert sum(r.status_code == 422 for r in responses) == 3
    assert sum(r.status_code == 429 for r in responses) == 13
    async with client._transport.app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count FROM abuse_hourly WHERE event='upload_throttled'") == 13


async def test_daily_denial_does_not_spend_hourly_capacity(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "rl_upload_day_times", 1)
    assert (await client.post("/sighting")).status_code == 422
    assert (await client.post("/capture")).status_code == 429
    async with client._transport.app.state.pool.acquire() as conn:
        assert {r["hits"] for r in await conn.fetch("SELECT hits FROM rate_limit_buckets")} == {1}


async def test_email_admission_is_shared_and_rolling(migrated_db, monkeypatch):
    monkeypatch.setattr(settings, "email_sender", "ses")
    monkeypatch.setattr(settings, "email_send_interval_s", 0)
    monkeypatch.setattr(settings, "email_daily_budget", 2)
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=6)
    async def attempt():
        async with pool.acquire() as conn:
            try:
                return await reserve(conn)
            except HTTPException as exc:
                assert exc.status_code == 429
                return None
    try:
        assert sum(r is not None for r in await asyncio.gather(*(attempt() for _ in range(12)))) == 2
    finally:
        await pool.close()
    with pytest.raises(HTTPException) as exc:
        await reserve(migrated_db)
    assert int(exc.value.headers["Retry-After"]) > 86000
    await migrated_db.execute("UPDATE email_send_attempts SET attempted_at=now()-interval '25 hours'")
    assert await reserve(migrated_db) is not None


async def test_email_rate_wait_does_not_extend_on_rejected_calls(migrated_db, monkeypatch):
    monkeypatch.setattr(settings, "email_sender", "ses")
    await reserve(migrated_db)
    before = await migrated_db.fetchval("SELECT next_at FROM email_delivery_state")
    with pytest.raises(HTTPException):
        await reserve(migrated_db)
    assert await migrated_db.fetchval("SELECT next_at FROM email_delivery_state") == before


async def test_transient_ses_failures_retry_without_persisting_links(migrated_db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "email_sender", "ses")
    calls = 0
    async def send():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "private-recipient@example.test"}}, "SendEmail")
    await deliver(migrated_db, send)
    assert calls == 2
    assert await migrated_db.fetchval("SELECT count(*) FROM email_send_attempts") == 2
    assert await migrated_db.fetchval("SELECT count(*) FROM email_send_attempts WHERE accepted") == 1
    assert "private-recipient" not in caplog.text
    columns = await migrated_db.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='email_send_attempts'")
    assert {r["column_name"] for r in columns} == {"id", "scope", "attempted_at", "accepted"}


async def test_daily_provider_throttle_is_persisted_without_a_long_request(migrated_db, monkeypatch):
    monkeypatch.setattr(settings, "email_sender", "ses")
    calls = 0
    async def send():
        nonlocal calls
        calls += 1
        raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "Daily message quota exceeded"}}, "SendEmail")
    with pytest.raises(HTTPException) as exc:
        await deliver(migrated_db, send)
    assert calls == 1
    assert int(exc.value.headers["Retry-After"]) >= 3600
    fresh = await asyncpg.connect(settings.test_database_url)
    try:
        with pytest.raises(HTTPException):
            await reserve(fresh)
    finally:
        await fresh.close()


async def test_permanent_provider_error_is_not_retried(migrated_db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "email_sender", "ses")
    calls = 0
    async def send():
        nonlocal calls
        calls += 1
        raise ClientError({"Error": {"Code": "MessageRejected", "Message": "private-reason@example.test"}}, "SendEmail")
    with pytest.raises(HTTPException) as exc:
        await deliver(migrated_db, send)
    assert exc.value.status_code == 503
    assert calls == 1
    assert "private-reason" not in caplog.text


async def test_provider_retry_ceiling_returns_a_wait_that_respects_inbox_cooldown(app_client, monkeypatch):
    monkeypatch.setattr(settings, "email_sender", "ses")
    monkeypatch.setattr(settings, "email_allowlist_domains", "example.test")
    calls = 0
    class Sender:
        async def send(self, address, link):
            nonlocal calls
            calls += 1
            raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "rate exceeded"}}, "SendEmail")
    monkeypatch.setattr("app.routes.join.get_sender", Sender)
    response = await app_client.post("/auth/email", data={"email": "person@example.test"}, headers={"Accept": "application/json"})
    assert calls == settings.email_send_attempts == 3
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= settings.auth_email_cooldown_s
    assert "60 seconds" in response.json()["error"]


async def test_capacity_denial_releases_address_cooldown_and_creates_no_account(app_client, monkeypatch):
    monkeypatch.setattr(settings, "email_sender", "ses")
    monkeypatch.setattr(settings, "email_allowlist_domains", "example.test")
    monkeypatch.setattr(settings, "email_daily_budget", 1)
    pool = app_client._transport.app.state.pool
    async with pool.acquire() as conn:
        await reserve(conn)
    response = await app_client.post("/auth/email", data={"email": "person@example.test"}, headers={"Accept": "application/json"})
    assert response.status_code == 429
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers") == 0
        await conn.execute("UPDATE email_send_attempts SET attempted_at=now()-interval '25 hours'")
        await conn.execute("UPDATE email_delivery_state SET next_at=now()-interval '1 second'")
    class Sender:
        async def send(self, address, link):
            pass
    monkeypatch.setattr("app.routes.join.get_sender", Sender)
    assert (await app_client.post("/auth/email", data={"email": "person@example.test"})).status_code == 200


async def test_creation_counters_commit_with_rows_and_ops_snapshot_is_readonly(migrated_db):
    oid = await get_or_create_observer_by_email(migrated_db, email="person@example.test")
    assert await get_or_create_observer_by_email(migrated_db, email="person@example.test") == oid
    assert await migrated_db.fetchval("SELECT count FROM abuse_hourly WHERE event='signups'") == 1
    with pytest.raises(RuntimeError):
        async with migrated_db.transaction():
            await get_or_create_observer_by_email(migrated_db, email="rollback@example.test")
            raise RuntimeError("rollback")
    assert await migrated_db.fetchval("SELECT count FROM abuse_hourly WHERE event='signups'") == 1
    await migrated_db.execute("INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1,$2,now())", uuid7(), oid)
    assert await migrated_db.fetchval("SELECT count FROM abuse_hourly WHERE event='uploads'") == 1
    async with migrated_db.transaction(readonly=True):
        report = await snapshot(migrated_db)
    assert report["hours"][0]["signups"] == 1
    assert report["hours"][0]["uploads"] == 1
    assert "person@example.test" not in json.dumps(report)


async def test_moderators_can_see_failed_signins_but_regular_accounts_cannot(authed_client):
    client, oid = authed_client
    assert (await client.get("/moderation/abuse")).status_code == 404
    await client.get("/auth/email/consume", params={"token": "bad"})
    async with client._transport.app.state.pool.acquire() as conn:
        await conn.execute("UPDATE observers SET trust_tier='moderator' WHERE id=$1", oid)
    response = await client.get("/moderation/abuse")
    assert response.status_code == 200
    assert response.json()["hours"][0]["failed_signins"] == 1


@pytest.mark.parametrize("sender,enabled", [("console", True), ("ses", False)])
async def test_open_signup_refuses_missing_sending_or_rate_controls(monkeypatch, sender, enabled):
    from app.main import lifespan
    monkeypatch.setattr(settings, "email_open_signup", True)
    monkeypatch.setattr(settings, "email_sender", sender)
    monkeypatch.setattr(settings, "rate_limit_enabled", enabled)
    with pytest.raises(RuntimeError, match="Open sign-up requires"):
        async with lifespan(FastAPI()):
            pytest.fail("unsafe configuration started")
