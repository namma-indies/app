"""Moderation/grievance mail delivery is visible in detailed health, not only the moderator tab."""
from unittest.mock import AsyncMock

import pytest

from app import moderator_alerts as alerts
from app.config import settings
from tests.test_operational_health import detail, health_client  # noqa: F401


async def _queue(pool, key="one"):
    async with pool.acquire() as conn:
        await alerts.queue_message(conn, dedupe_key=key, recipient="mod@example.invalid",
                                   subject="Safe", text="Safe", html="<p>Safe</p>", urgent=True)


@pytest.mark.parametrize("setting,value", [("moderator_alert_emails", ""), ("email_sender", "console")])
async def test_unconfigured_delivery_degrades_health(health_client, monkeypatch, setting, value):  # noqa: F811
    monkeypatch.setattr(settings, setting, value)
    response = await detail(health_client)
    assert response.status_code == 503
    assert response.json()["checks"]["moderation_mail"]["status"] == "unconfigured"


async def test_configured_idle_delivery_is_ok(health_client):  # noqa: F811
    check = (await detail(health_client)).json()["checks"]["moderation_mail"]
    assert check["status"] == "ok" and check["oldest_unsent_age_s"] is None


async def test_old_unsent_mail_is_stale_with_its_age(health_client):  # noqa: F811
    pool = health_client._transport.app.state.pool
    await _queue(pool)
    assert (await detail(health_client)).json()["checks"]["moderation_mail"]["status"] == "ok"
    async with pool.acquire() as conn:
        await conn.execute("UPDATE moderation_mail SET created_at=now()-interval '2 hours'")
    response = await detail(health_client)
    check = response.json()["checks"]["moderation_mail"]
    assert response.status_code == 503
    assert check["status"] == "stale" and check["oldest_unsent_age_s"] >= 7200 and check["pending"] == 1
    assert "mod@example.invalid" not in response.text


async def test_permanent_errors_stop_retrying_and_surface(health_client, monkeypatch):  # noqa: F811
    pool = health_client._transport.app.state.pool
    monkeypatch.setattr(settings, "moderation_mail_max_attempts", 3)
    sender = AsyncMock()
    sender.send_message.side_effect = ValueError("MessageRejected: details")
    monkeypatch.setattr(alerts, "get_sender", lambda: sender)
    await _queue(pool)
    for _ in range(3):
        assert await alerts.deliver_one(pool)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE moderation_mail SET available_at=now()-interval '1 second'")
    # Given up: never picked again, however long we wait.
    assert not await alerts.deliver_one(pool)
    assert sender.send_message.await_count == 3
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT failed_at, sent_at, attempts FROM moderation_mail")
        status = await alerts.mail_status(conn)
    assert row["failed_at"] is not None and row["sent_at"] is None and row["attempts"] == 3
    assert status["undeliverable_mail"] == 1 and status["pending_mail"] == 0 and status["failed_mail"] == 0
    check = (await detail(health_client)).json()["checks"]["moderation_mail"]
    assert check["status"] == "failed" and check["undeliverable"] == 1
    # Outside the window it stays counted but no longer pages.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE moderation_mail SET failed_at=now()-interval '2 days'")
    check = (await detail(health_client)).json()["checks"]["moderation_mail"]
    assert check["status"] == "ok" and check["undeliverable"] == 1


async def test_moderator_status_reports_undeliverable_count(authed_client, monkeypatch):
    from tests.test_moderation import _make_moderator, _pool

    client, oid = authed_client
    pool = await _pool(client)
    await _queue(pool)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE moderation_mail SET failed_at=now(), attempts=8, last_error='ValueError'")
    await _make_moderator(client, oid)
    status = (await client.get("/moderation/alerts/status")).json()
    assert status["undeliverable_mail"] == 1 and status["failed_mail"] == 0 and status["pending_mail"] == 0
