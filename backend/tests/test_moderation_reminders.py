"""Escalation as severe-report and urgent-grievance clocks run down."""
from app import moderator_alerts as alerts
from app.config import settings
from tests.test_grievances import form
from tests.test_moderation import _make_moderator, _pool, _post, _report
from tests.test_operational_health import detail, health_client  # noqa: F401


async def _reminders(conn):
    return sorted((r["category"], r["urgent"]) for r in await conn.fetch(
        "SELECT category, urgent FROM moderator_alerts WHERE event_key LIKE 'reminder:%'"))


async def test_severe_report_gets_one_due_soon_and_one_overdue_reminder(authed_client, monkeypatch):
    client, oid = authed_client
    monkeypatch.setattr(settings, "moderation_severe_response_hours", 2)
    monkeypatch.setattr(settings, "moderation_reminder_fraction", 0.75)
    sid = await _post(client)
    ordinary = await _post(client)
    await _report(client, sid, reason="people_privacy")
    await _report(client, ordinary, reason="other")
    async with (await _pool(client)).acquire() as conn:
        assert await alerts.escalate_deadlines(conn) == 0
        # 1h of 2h elapsed: not yet at 75%.
        await conn.execute("UPDATE sighting_reports SET opened_at=opened_at-interval '1 hour', "
                           "severe_at=severe_at-interval '1 hour'")
        assert await alerts.escalate_deadlines(conn) == 0
        await conn.execute("UPDATE sighting_reports SET opened_at=opened_at-interval '40 minutes', "
                           "severe_at=severe_at-interval '40 minutes'")
        assert await alerts.escalate_deadlines(conn) == 1
        assert await alerts.escalate_deadlines(conn) == 0  # idempotent
        assert await _reminders(conn) == [("report_severe_due_soon", True)]
        await conn.execute("UPDATE sighting_reports SET opened_at=opened_at-interval '1 hour', "
                           "severe_at=severe_at-interval '1 hour'")
        assert await alerts.escalate_deadlines(conn) == 1
        assert await alerts.escalate_deadlines(conn) == 0
        assert await _reminders(conn) == [("report_severe_due_soon", True), ("report_severe_overdue", True)]
    # A ruling closes the episode: nothing further.
    await _make_moderator(client, oid)
    await client.post(f"/sighting/{sid}/review", data={"verdict": "rejected", "reason": "Removed"})
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("DELETE FROM moderator_alerts")
        assert await alerts.escalate_deadlines(conn) == 0


async def test_item_first_seen_overdue_gets_only_the_overdue_reminder(authed_client):
    client, _ = authed_client
    sid = await _post(client)
    await _report(client, sid, reason="illegal_content")
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("UPDATE sighting_reports SET opened_at=now()-interval '3 days', severe_at=now()-interval '3 days'")
        assert await alerts.escalate_deadlines(conn) == 1
        assert await _reminders(conn) == [("report_severe_overdue", True)]


async def test_urgent_grievance_reminders_and_overdue_health(health_client):  # noqa: F811
    client = health_client
    assert (await client.post("/grievances", data=form(kind="intimate_imagery"))).status_code == 201
    assert (await client.post("/grievances", data=form(kind="other"))).status_code == 201
    pool = await _pool(client)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE moderation_mail SET sent_at=now()")  # acknowledged
        assert await alerts.escalate_deadlines(conn) == 0
    check = (await detail(client)).json()["checks"]["grievances"]
    assert check == {"status": "ok", "open": 2, "overdue": 0}
    async with pool.acquire() as conn:
        # 2-hour removal clock, 1h45m elapsed.
        await conn.execute("UPDATE grievances SET received_at=received_at-interval '105 minutes', "
                           "removal_due_at=removal_due_at-interval '105 minutes', "
                           "resolve_due_at=resolve_due_at-interval '105 minutes'")
        assert await alerts.escalate_deadlines(conn) == 1
        assert await _reminders(conn) == [("grievance_removal_due_soon", True)]
        await conn.execute("UPDATE grievances SET received_at=received_at-interval '1 hour', "
                           "removal_due_at=removal_due_at-interval '1 hour', "
                           "resolve_due_at=resolve_due_at-interval '1 hour'")
        assert await alerts.escalate_deadlines(conn) == 1
    response = await detail(client)
    assert response.status_code == 503
    assert response.json()["checks"]["grievances"] == {"status": "overdue", "open": 2, "overdue": 1}
    async with pool.acquire() as conn:
        assert await _reminders(conn) == [("grievance_removal_due_soon", True), ("grievance_removal_overdue", True)]
        # Reminder mail routes to the grievance queue and carries no complaint content.
        await conn.execute("UPDATE moderator_alerts SET queued_at=now() WHERE event_key NOT LIKE 'reminder:%'")
        assert await alerts.prepare_alerts(conn) == 2
        body = await conn.fetchval("SELECT body_text FROM moderation_mail WHERE subject LIKE '%urgent%'")
    assert "queue=grievances" in body and "private review" not in body and "person@example.invalid" not in body
