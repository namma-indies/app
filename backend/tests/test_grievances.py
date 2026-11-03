"""No-account intake, durable acknowledgement, provisional clocks and human decisions."""
import asyncio
import re
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import settings
from app import moderator_alerts
from tests.test_moderation import _post, _pool, _make_moderator, _become


def form(**overrides):
    return {"submission_key": str(uuid4()), "kind": "privacy", "target": str(uuid4()),
            "details": "This needs a private review.", "contact_email": "person@example.invalid", **overrides}


def reference(response):
    return re.search(r"NI-[A-F0-9]{32}", response.text)[0]


async def latest(client):
    async with (await _pool(client)).acquire() as conn:
        return await conn.fetchrow("SELECT * FROM grievances ORDER BY received_at DESC LIMIT 1")


async def test_public_page_has_honest_placeholders_and_all_intake_types(app_client, monkeypatch):
    for name in ("grievance_officer_name", "grievance_officer_email", "grievance_officer_contact"):
        monkeypatch.setattr(settings, name, "")
    response = await app_client.get("/grievances")
    assert response.status_code == 200
    for label in ("Not yet appointed", "Contact details pending", "Takedown", "Privacy",
                  "Non-consensual intimate imagery", "Government or court order", "Other",
                  "without an account", "confirm every deadline"):
        assert label in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_officer_settings_are_read_and_escaped(app_client, monkeypatch):
    monkeypatch.setattr(settings, "grievance_officer_name", "<script>Officer</script>")
    monkeypatch.setattr(settings, "grievance_officer_email", "officer@example.invalid")
    monkeypatch.setattr(settings, "grievance_officer_contact", "Contact through the form")
    page = (await app_client.get("/grievances")).text
    assert "&lt;script&gt;Officer&lt;/script&gt;" in page and "<script>Officer" not in page
    assert "officer@example.invalid" in page and "Contact through the form" in page


@pytest.mark.parametrize(("kind", "hours"), [("takedown", 36), ("privacy", 36),
    ("intimate_imagery", 2), ("government_order", 3), ("other", None)])
async def test_no_account_is_created_and_deadlines_are_snapshotted(app_client, kind, hours):
    response = await app_client.post("/grievances", data=form(kind=kind, contact_email=" PERSON@EXAMPLE.INVALID "))
    assert response.status_code == 201, response.text
    row = await latest(app_client)
    assert reference(response) == row["reference"] and row["contact_email"] == "person@example.invalid"
    assert row["acknowledge_due_at"] - row["received_at"] == timedelta(hours=24)
    assert row["resolve_due_at"] - row["received_at"] == timedelta(days=7)
    if hours is None:
        assert row["removal_due_at"] is None
    else:
        assert row["removal_due_at"] - row["received_at"] == timedelta(hours=hours)
    async with (await _pool(app_client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM observers") == 0
        mail = await conn.fetchrow("SELECT * FROM moderation_mail")
        assert row["reference"] in mail["body_text"] and mail["sent_at"] is None
        assert row["details"] not in mail["body_text"] and row["target"] not in mail["body_text"]
        assert await conn.fetchval("SELECT urgent FROM moderator_alerts") == (kind != "other")


async def test_duplicate_and_concurrent_submissions_share_one_reference_and_email(app_client):
    body = form()
    responses = await asyncio.gather(*(app_client.post("/grievances", data=body) for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 201]
    assert reference(responses[0]) == reference(responses[1])
    assert (await app_client.post("/grievances", data=body)).status_code == 200
    async with (await _pool(app_client)).acquire() as conn:
        for table in ("grievances", "moderation_mail", "moderator_alerts"):
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 1
    changed = await app_client.post("/grievances", data={**body, "details": "Changed"})
    assert changed.status_code == 409 and reference(responses[0]) not in changed.text


async def test_intake_and_both_notifications_roll_back_together(app_client, monkeypatch):
    monkeypatch.setattr("app.routes.grievances.alert_moderators", AsyncMock(side_effect=RuntimeError("queue failed")))
    with pytest.raises(RuntimeError):
        await app_client.post("/grievances", data=form())
    async with (await _pool(app_client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM grievances") == 0
        assert await conn.fetchval("SELECT count(*) FROM moderation_mail") == 0


@pytest.mark.parametrize("fields", [{"kind": "unknown"}, {"target": "javascript:alert(1)"},
    {"details": "  "}, {"details": "x" * 5001}, {"contact_email": "bad\r\naddress@example.invalid"}])
async def test_invalid_intake_is_refused_without_mail(app_client, fields):
    response = await app_client.post("/grievances", data=form(**fields))
    assert response.status_code == 422
    async with (await _pool(app_client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM grievances") == 0
        assert await conn.fetchval("SELECT count(*) FROM moderation_mail") == 0


async def test_form_errors_escape_values_and_accept_unicode_details(app_client):
    body = form(details='</textarea><script>alert("unsafe")</script>', contact_email="invalid")
    response = await app_client.post("/grievances", data=body)
    assert "&lt;script&gt;" in response.text and "<script>" not in response.text
    response = await app_client.post("/grievances", data=form(details="ನ" * 5000))
    assert response.status_code == 201


async def test_body_is_bounded_and_only_form_fields_are_accepted(app_client):
    r = await app_client.post("/grievances", content=b"x" * (128 * 1024 + 1), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert (await app_client.post("/grievances", json=form())).status_code == 415
    r = await app_client.post("/grievances", content="kind=privacy&kind=other", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400


async def test_public_intake_limits_address_and_ip(app_client, monkeypatch):
    monkeypatch.setattr(settings, "rl_grievance_email_times", 1)
    assert (await app_client.post("/grievances", data=form())).status_code == 201
    r = await app_client.post("/grievances", data=form())
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # A separate address still has an IP budget, as on the existing email path.
    monkeypatch.setattr(settings, "rl_grievance_ip_times", 2)
    assert (await app_client.post("/grievances", data=form(contact_email="other@example.invalid"))).status_code == 429


async def test_queue_and_actions_are_moderator_only(authed_client):
    client, _ = authed_client
    await client.post("/grievances", data=form())
    row = await latest(client)
    assert (await client.get("/moderation/grievances")).status_code == 404
    assert (await client.post(f"/moderation/grievances/{row['id']}/action", json={"action": "dismiss", "reason": "Reviewed"})).status_code == 404
    client.cookies.clear()
    assert (await client.get("/moderation/grievances")).status_code == 401


async def test_clocks_and_email_failure_remain_visible_after_resolution(authed_client, monkeypatch):
    client, oid = authed_client
    await _make_moderator(client, oid)
    await client.post("/grievances", data=form())
    row = await latest(client)
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("UPDATE grievances SET received_at=now()-interval '10 days',acknowledge_due_at=now()-interval '9 days',"
                           "resolve_due_at=now()-interval '3 days',removal_due_at=now()-interval '9 days'")
        await conn.execute("UPDATE moderation_mail SET last_error='TimeoutError'")
        original_due = await conn.fetchval("SELECT acknowledge_due_at FROM grievances")
    monkeypatch.setattr(settings, "grievance_acknowledge_hours", 72)
    item = (await client.get("/moderation/grievances")).json()["items"][0]
    assert item["acknowledgement_overdue"] and item["resolution_overdue"] and item["removal_overdue"]
    assert item["acknowledgement_error"] == "TimeoutError" and item["legal_confirmation_required"]
    assert "submission_key" not in item and "payload_digest" not in item
    r = await client.post(f"/moderation/grievances/{row['id']}/action", json={"action": "resolve", "reason": "Reply sent; concern addressed"})
    assert r.status_code == 200
    item = (await client.get("/moderation/grievances")).json()["items"][0]
    assert item["status"] == "resolved" and item["acknowledgement_overdue"]
    assert datetime.fromisoformat(item["acknowledge_due_at"].replace("Z", "+00:00")) == original_due
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("UPDATE moderation_mail SET sent_at=now(),last_error=NULL")
    assert (await client.get("/moderation/grievances")).json()["items"] == []
    found = (await client.get("/moderation/grievances", params={"reference": row["reference"].lower()})).json()["items"]
    assert found[0]["resolution_reason"] == "Reply sent; concern addressed"


async def test_grievance_does_not_auto_hide_and_human_hide_covers_shared_capture(app_client):
    from tests.test_multi_captures import result, seed
    from app.capture_jobs import complete_capture_job
    async with (await _pool(app_client)).acquire() as conn:
        body = result()
        oid, cid, jid, storage = await seed(conn, body)
        await complete_capture_job(conn, storage, jid, body)
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM sightings WHERE capture_id=$1 ORDER BY id", cid)]
    _become(app_client, oid)
    await _make_moderator(app_client, oid)
    await app_client.post("/grievances", data=form(kind="government_order", target=str(ids[0])))
    row = await latest(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1 AND review_status='valid'", cid) == 2
    r = await app_client.post(f"/moderation/grievances/{row['id']}/action", json={"action": "hide", "reason": "Order verified; content held"})
    assert r.status_code == 200, r.text
    async with (await _pool(app_client)).acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1 AND review_status='rejected'", cid) == 2
        assert await conn.fetchval("SELECT count(*) FROM moderation_actions WHERE action='hide'") == 3
        record = await conn.fetchrow("SELECT * FROM grievances")
        assert record["removed_at"] is not None and record["resolved_at"] is None


async def test_grievance_action_and_sighting_hold_are_atomic(authed_client, monkeypatch):
    client, oid = authed_client
    sid = await _post(client)
    await _make_moderator(client, oid)
    await client.post("/grievances", data=form(target=sid))
    row = await latest(client)
    monkeypatch.setattr("app.routes.grievances.record_action", AsyncMock(side_effect=RuntimeError("ledger unavailable")))
    with pytest.raises(RuntimeError):
        await client.post(f"/moderation/grievances/{row['id']}/action", json={"action": "hide", "reason": "Private details visible"})
    async with (await _pool(client)).acquire() as conn:
        assert await conn.fetchval("SELECT review_status FROM sightings WHERE id=$1::uuid", sid) == "valid"
        assert await conn.fetchval("SELECT removed_at FROM grievances") is None
        assert await conn.fetchval("SELECT count(*) FROM moderation_actions") == 0


async def test_decisions_require_reason_and_cannot_overwrite_a_closed_complaint(authed_client):
    client, oid = authed_client
    await _make_moderator(client, oid)
    await client.post("/grievances", data=form())
    row = await latest(client)
    path = f"/moderation/grievances/{row['id']}/action"
    for reason in ("", " ", "x" * 501):
        assert (await client.post(path, json={"action": "dismiss", "reason": reason})).status_code == 422
    assert (await client.post(path, json={"action": "hide", "reason": "Check"})).status_code == 404
    assert (await client.post(path, json={"action": "dismiss", "reason": "Unrelated complaint; reply sent"})).status_code == 200
    assert (await client.post(path, json={"action": "resolve", "reason": "Second ruling"})).status_code == 409


async def test_acknowledgement_delivery_and_moderator_alert_use_existing_mailer(authed_client, monkeypatch):
    client, oid = authed_client
    await _make_moderator(client, oid)
    monkeypatch.setattr(settings, "moderator_alert_emails", "moderator@example.invalid")
    monkeypatch.setattr(settings, "email_sender", "ses")
    sender = AsyncMock()
    monkeypatch.setattr(moderator_alerts, "get_sender", lambda: sender)
    await client.post("/grievances", data=form(kind="intimate_imagery", details="Evidence stays private"))
    pool = await _pool(client)
    assert await moderator_alerts.deliver_one(pool)
    assert sender.send_message.await_args.args[0] == "person@example.invalid"
    async with pool.acquire() as conn:
        assert await moderator_alerts.prepare_alerts(conn) == 1
        mail = await conn.fetchrow("SELECT * FROM moderation_mail WHERE sent_at IS NULL")
        assert "queue=grievances" in mail["body_text"]
        assert "Evidence stays private" not in mail["body_text"] and "person@example.invalid" not in mail["body_text"]
    item = (await client.get("/moderation/grievances")).json()["items"][0]
    assert item["acknowledged_at"] is not None and not item["acknowledgement_overdue"]


async def test_photo_erasure_keeps_the_complaint_and_opaque_reference(authed_client):
    from app.erasure import erase_sighting
    from uuid import UUID
    client, oid = authed_client
    sid = await _post(client)
    await _make_moderator(client, oid)
    await client.post("/grievances", data=form(target=sid))
    complaint = await latest(client)
    async with (await _pool(client)).acquire() as conn:
        async with conn.transaction():
            await erase_sighting(conn, UUID(sid), reason="test erasure", owner=oid)
        assert await conn.fetchval("SELECT sighting_id FROM grievances") == UUID(sid)
    response = await client.get("/moderation/grievances", params={"reference": complaint["reference"]})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["photos"] == []
