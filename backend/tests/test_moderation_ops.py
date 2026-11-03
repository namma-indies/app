"""Launch moderation: serious concerns reach a person, with durable clocks and history."""
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.ids import uuid7
from app import moderator_alerts as alerts
from tests.test_moderation import _post, _report, _pool, _make_moderator, _observer, _become, _status


@pytest.mark.parametrize("reason", ["people_privacy", "sexual_content", "illegal_content"])
async def test_severe_report_holds_after_one_report_and_alerts_now(authed_client, reason):
    client, oid = authed_client
    sid = await _post(client)
    r = await _report(client, sid, reason=reason, note="Private evidence stays in the queue")
    assert r.json()["hidden"] is True
    assert (await client.get("/map")).json()["sightings"] == []
    async with (await _pool(client)).acquire() as conn:
        event = await conn.fetchrow("SELECT * FROM moderator_alerts")
        assert event["urgent"] and event["available_at"] == event["created_at"]
        assert event["category"] == f"report_{reason}"
    await _make_moderator(client, oid)
    item = (await client.get("/moderation/queue")).json()["items"][0]
    assert item["severe"] and item["age_seconds"] >= 0 and not item["overdue"]


async def test_duplicate_report_preserves_clock_and_does_not_repeat_alert(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid, reason="people_privacy")
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("UPDATE sighting_reports SET opened_at=now()-interval '3 hours', severe_at=now()-interval '3 hours'")
        first = await conn.fetchval("SELECT opened_at FROM sighting_reports")
    await _report(client, sid, reason="people_privacy")
    await _report(client, sid, reason="people_privacy", note="Extra context")
    async with (await _pool(client)).acquire() as conn:
        assert await conn.fetchval("SELECT opened_at FROM sighting_reports") == first
        assert await conn.fetchval("SELECT count(*) FROM moderator_alerts") == 1
    await _make_moderator(client, oid)
    item = (await client.get("/moderation/queue")).json()["items"][0]
    assert item["overdue"] and item["age_seconds"] >= 3 * 3600
    await _report(client, sid, reason="other")
    changed = (await client.get("/moderation/queue")).json()["items"][0]
    assert changed["severe"] and changed["response_due_at"] == item["response_due_at"]


async def test_severe_escalation_bypasses_digest_and_orders_ahead_of_volume(authed_client):
    client, oid = authed_client
    ordinary, serious = await _post(client), await _post(client)
    await _report(client, ordinary)
    await _report(client, serious, reason="other")
    _become(client, await _observer(client))
    await _report(client, ordinary)
    _become(client, oid)
    await _report(client, serious, reason="illegal_content")
    await _make_moderator(client, oid)
    items = (await client.get("/moderation/queue")).json()["items"]
    assert [i["sighting_id"] for i in items] == [serious, ordinary]
    async with (await _pool(client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM moderator_alerts WHERE urgent") == 1


async def test_severe_concern_after_approval_holds_again_with_a_new_clock(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid, reason="people_privacy")
    await _make_moderator(client, oid)
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("UPDATE sighting_reports SET opened_at=now()-interval '3 hours',severe_at=now()-interval '3 hours'")
        old_opened = await conn.fetchval("SELECT opened_at FROM sighting_reports")
    assert (await client.post(f"/sighting/{sid}/review", data={"verdict": "valid", "reason": "Concern addressed"})).status_code == 200
    assert (await client.get("/moderation/queue")).json()["items"] == []
    await _report(client, sid, reason="sexual_content")
    assert await _status(client, sid) == "pending"
    item = (await client.get("/moderation/queue")).json()["items"][0]
    assert item["reported_after_review"]
    async with (await _pool(client)).acquire() as conn:
        assert await conn.fetchval("SELECT opened_at FROM sighting_reports") > old_opened
        assert await conn.fetchval("SELECT count(*) FROM moderator_alerts") == 2


async def test_every_moderator_action_records_who_when_and_why(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _make_moderator(client, oid)
    for action in ("hide", "unhide", "approve", "reject", "dismiss"):
        verdict = "rejected" if action in {"hide", "reject"} else "valid"
        result = await client.post(f"/sighting/{sid}/review", data={"verdict": verdict, "action": action, "reason": f"Because: {action}"})
        assert result.status_code == 200, result.text
    target = await _observer(client)
    assert (await client.post(f"/moderation/observer/{target}/ban", json={"reason": "Repeated abuse"})).status_code == 200
    assert (await client.post(f"/moderation/observer/{target}/unban", json={"reason": "Appeal upheld"})).status_code == 200
    entries = (await client.get("/moderation/audit")).json()["items"]
    assert {a["action"] for a in entries} == {"hide", "unhide", "approve", "reject", "dismiss", "ban", "unban"}
    assert all(a["moderator_id"] == str(oid) and a["created_at"] and a["reason"] for a in entries)
    async with (await _pool(client)).acquire() as conn:
        for permission in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not await conn.fetchval("SELECT has_table_privilege('app_rw','moderation_actions',$1)", permission)


async def test_reason_required_and_action_must_agree_with_verdict(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _make_moderator(client, oid)
    for fields in ({}, {"reason": "  "}, {"reason": "x" * 501}, {"reason": "ok", "action": "hide"}):
        r = await client.post(f"/sighting/{sid}/review", data={"verdict": "valid", **fields})
        assert r.status_code == 422
    assert (await client.post(f"/moderation/observer/{await _observer(client)}/ban", json={})).status_code == 422
    assert (await client.get("/moderation/audit")).json()["items"] == []


async def test_audit_failure_rolls_back_the_moderator_decision(authed_client, monkeypatch):
    client, oid = authed_client
    sid = await _post(client)
    await _make_moderator(client, oid)
    monkeypatch.setattr("app.routes.moderation.record_action", AsyncMock(side_effect=RuntimeError("audit failed")))
    with pytest.raises(RuntimeError):
        await client.post(f"/sighting/{sid}/review", data={"verdict": "rejected", "reason": "Evidence reviewed"})
    assert await _status(client, sid) == "valid"


async def test_audit_and_delivery_status_are_moderator_only(authed_client):
    client, _ = authed_client
    for path in ("/moderation/audit", "/moderation/alerts/status"):
        assert (await client.get(path)).status_code == 404


async def test_alerts_are_transactional_and_idempotent(migrated_db):
    conn = migrated_db
    with pytest.raises(RuntimeError):
        async with conn.transaction():
            await alerts.alert_moderators(conn, event_key="rollback", category="upload_screening", subject_id=uuid7(), urgent=True)
            raise RuntimeError()
    assert await conn.fetchval("SELECT count(*) FROM moderator_alerts") == 0
    for _ in range(2):
        await alerts.alert_moderators(conn, event_key="held-upload:one", category="upload_screening", subject_id=uuid7(), urgent=True)
    assert await conn.fetchval("SELECT count(*) FROM moderator_alerts") == 1


async def test_digest_waits_but_urgent_is_prepared_now(migrated_db, monkeypatch):
    conn = migrated_db
    monkeypatch.setattr(settings, "moderator_alert_emails", "mod@example.invalid")
    await alerts.alert_moderators(conn, event_key="ordinary", category="report_other", subject_id=uuid7())
    await alerts.alert_moderators(conn, event_key="urgent", category="report_people_privacy", subject_id=uuid7(), urgent=True)
    assert await alerts.prepare_alerts(conn) == 1
    assert await conn.fetchval("SELECT count(*) FROM moderation_mail") == 1
    assert await conn.fetchval("SELECT urgent FROM moderation_mail")
    await conn.execute("UPDATE moderator_alerts SET available_at=now()-interval '1 second' WHERE NOT urgent")
    assert await alerts.prepare_alerts(conn) == 1
    assert await alerts.prepare_alerts(conn) == 0
    assert await conn.fetchval("SELECT count(*) FROM moderation_mail") == 2


async def test_missing_recipients_preserves_events_and_reports_unconfigured(authed_client, monkeypatch):
    client, oid = authed_client
    monkeypatch.setattr(settings, "moderator_alert_emails", "")
    await _report(client, await _post(client), reason="people_privacy")
    async with (await _pool(client)).acquire() as conn:
        assert await alerts.prepare_alerts(conn) == 0
    await _make_moderator(client, oid)
    status = (await client.get("/moderation/alerts/status")).json()
    assert not status["delivery_configured"] and status["pending_events"] == 1


async def test_failed_mail_retries_without_resending_successful_recipient(authed_client, monkeypatch):
    client, _ = authed_client
    pool = await _pool(client)
    monkeypatch.setattr(settings, "email_sender", "ses")
    monkeypatch.setattr(settings, "moderator_alert_emails", "a@example.invalid,b@example.invalid")
    sender = AsyncMock()
    sender.send_message.side_effect = [None, ValueError("sensitive provider details"), None]
    monkeypatch.setattr(alerts, "get_sender", lambda: sender)
    async with pool.acquire() as conn:
        await alerts.alert_moderators(conn, event_key="one", category="upload_screening", subject_id=uuid7(), urgent=True)
        await alerts.prepare_alerts(conn)
    assert await alerts.deliver_one(pool)
    assert await alerts.deliver_one(pool)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM moderation_mail WHERE sent_at IS NOT NULL") == 1
        assert await conn.fetchval("SELECT last_error FROM moderation_mail WHERE sent_at IS NULL") == "ValueError"
        await conn.execute("UPDATE moderation_mail SET available_at=now()-interval '1 second' WHERE sent_at IS NULL")
    assert await alerts.deliver_one(pool)
    assert [call.args[0] for call in sender.send_message.await_args_list] == ["a@example.invalid", "b@example.invalid", "b@example.invalid"]
    assert not await alerts.deliver_one(pool)


async def test_expired_delivery_lease_is_recovered_and_urgent_precedes_digest(authed_client, monkeypatch):
    client, _ = authed_client
    pool = await _pool(client)
    monkeypatch.setattr(settings, "email_sender", "ses")
    sender = AsyncMock()
    monkeypatch.setattr(alerts, "get_sender", lambda: sender)
    async with pool.acquire() as conn:
        for key, urgent in (("digest", False), ("urgent", True)):
            await alerts.queue_message(conn, dedupe_key=key, recipient="mod@example.invalid", subject=key, text="Safe", html="<p>Safe</p>", urgent=urgent)
        await conn.execute("UPDATE moderation_mail SET lease_until=now()-interval '1 second',lease_token=$1 WHERE urgent", uuid7())
    assert await alerts.deliver_one(pool)
    assert sender.send_message.await_args.args[1] == "urgent"
    assert await alerts.deliver_one(pool)
    assert sender.send_message.await_args.args[1] == "digest"


async def test_console_mode_keeps_mail_pending_until_real_delivery_is_configured(authed_client, monkeypatch):
    client, _ = authed_client
    pool = await _pool(client)
    monkeypatch.setattr(settings, "email_sender", "console")
    sender = AsyncMock()
    monkeypatch.setattr(alerts, "get_sender", lambda: sender)
    async with pool.acquire() as conn:
        await alerts.queue_message(conn, dedupe_key="pending", recipient="mod@example.invalid", subject="Pending", text="Pending", html="<p>Pending</p>")
    assert not await alerts.deliver_one(pool)
    sender.send_message.assert_not_awaited()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT sent_at FROM moderation_mail") is None
        assert await conn.fetchval("SELECT attempts FROM moderation_mail") == 0


async def test_severe_reason_edits_emit_one_urgent_alert_per_open_episode(authed_client):
    client, oid = authed_client
    sid = await _post(client)
    await _report(client, sid, reason="other")
    for reason in ("people_privacy", "sexual_content", "illegal_content") * 3:
        await _report(client, sid, reason=reason)
    async with (await _pool(client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM moderator_alerts WHERE urgent") == 1
        assert await conn.fetchval("SELECT count(*) FROM moderator_alerts WHERE NOT urgent") == 1
    await _make_moderator(client, oid)
    await client.post(f"/sighting/{sid}/review", data={"verdict": "valid", "reason": "Concern assessed"})
    for reason in ("sexual_content", "people_privacy", "illegal_content") * 2:
        await _report(client, sid, reason=reason)
    async with (await _pool(client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM moderator_alerts WHERE urgent") == 2
