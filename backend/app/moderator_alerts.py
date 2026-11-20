"""Durable moderator alerts and mail delivery through the existing sender.

`alert_moderators` is the integration seam for reports, upload screening and
grievances. Enqueue it in the transaction that creates the work to be reviewed.
Delivery is at least once: a crash after the provider accepts mail can duplicate
that one message, but cannot lose the event. No evidence or contact data belongs
in an alert; moderators follow the authenticated queue link.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import UUID

from app.config import settings
from app.email.sender import get_sender
from app.ids import uuid7

logger = logging.getLogger(__name__)
MAIL_TIMEOUT_S = 20
MAIL_LEASE_S = 90
# India has no daylight saving, so a fixed offset is exact and needs no tz
# database in the runtime image.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def local_time(moment: datetime) -> str:
    """Moderators read alerts in Asia/Kolkata; keep the offset explicit."""
    return moment.astimezone(IST).strftime("%d %b %Y, %H:%M IST (UTC+05:30)")


async def alert_moderators(conn, *, event_key: str, category: str, subject_id: UUID,
                           urgent: bool = False) -> None:
    """Persist once per stable event_key; urgent bypasses the ordinary digest.

    category is a short machine label (for example upload_screening), never
    user-written evidence. Retrying with the same key is safe.
    """
    if not 1 <= len(event_key) <= 200 or not 1 <= len(category) <= 80:
        raise ValueError("alert key or category outside the supported length")
    await conn.execute(
        "INSERT INTO moderator_alerts (id,event_key,category,subject_id,urgent,available_at) "
        "VALUES ($1,$2,$3,$4,$5,CASE WHEN $5 THEN now() ELSE "
        "to_timestamp((floor(extract(epoch FROM now()) / $6) + 1) * $6) END) "
        "ON CONFLICT (event_key) DO NOTHING",
        uuid7(), event_key, category, subject_id, urgent,
        float(settings.moderation_digest_minutes * 60))


async def queue_message(conn, *, dedupe_key: str, recipient: str, subject: str,
                        text: str, html: str, urgent: bool = False) -> UUID:
    """Shared durable mail outbox (also used for grievance acknowledgements)."""
    return await conn.fetchval(
        "INSERT INTO moderation_mail (id,dedupe_key,recipient,subject,body_text,body_html,urgent) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (dedupe_key) DO UPDATE "
        "SET dedupe_key = EXCLUDED.dedupe_key RETURNING id",
        uuid7(), dedupe_key, recipient, subject, text, html, urgent)


def recipients() -> list[str]:
    return sorted({s.strip().lower() for s in settings.moderator_alert_emails.split(",") if s.strip()})


def delivery_configured() -> bool:
    return bool(recipients()) and settings.email_sender == "ses"


MAIL_STATUS_SQL = """
    SELECT (SELECT count(*) FROM moderator_alerts WHERE queued_at IS NULL) AS pending_events,
           count(*) FILTER (WHERE sent_at IS NULL AND failed_at IS NULL) AS pending_mail,
           count(*) FILTER (WHERE sent_at IS NULL AND failed_at IS NULL AND attempts>0
                            AND last_error IS NOT NULL) AS failed_mail,
           count(*) FILTER (WHERE sent_at IS NULL AND failed_at IS NOT NULL) AS undeliverable_mail,
           count(*) FILTER (WHERE sent_at IS NULL AND failed_at >= now() - $1 * interval '1 second')
               AS recently_undeliverable_mail,
           extract(epoch FROM now() - min(created_at) FILTER (WHERE sent_at IS NULL AND failed_at IS NULL))
               AS oldest_unsent_age_s
    FROM moderation_mail
"""


async def mail_status(conn) -> dict:
    """Counts only: no recipients, subjects or bodies leave this function."""
    row = await conn.fetchrow(MAIL_STATUS_SQL, float(settings.health_moderation_mail_failed_window_s))
    result = dict(row)
    age = result.pop("oldest_unsent_age_s")
    result["oldest_unsent_age_s"] = max(0, int(age)) if age is not None else None
    return result


async def prepare_alerts(conn) -> int:
    """Atomically expand due events into one digest per recipient and urgency."""
    addresses = recipients()
    if not addresses:
        return 0  # Keep events until a recipient is configured.
    async with conn.transaction():
        rows = await conn.fetch(
            "SELECT * FROM moderator_alerts WHERE queued_at IS NULL AND available_at <= now() "
            "ORDER BY urgent DESC, available_at, id FOR UPDATE SKIP LOCKED LIMIT 200")
        for urgent in (True, False):
            batch = [r for r in rows if r["urgent"] == urgent]
            if not batch:
                continue
            subject = "IndieDex: urgent moderation review" if urgent else "IndieDex: moderation digest"
            root = settings.public_base_url.rstrip("/") + "/?tab=moderation"
            grievances = any(r["category"].startswith("grievance_") for r in batch)
            only_grievances = all(r["category"].startswith("grievance_") for r in batch)
            link = root + "&queue=grievances" if only_grievances else root
            # Counts, machine labels and opaque IDs only: never forward reports,
            # images, precise locations or complainant addresses by email.
            lines = [f"{r['category']}: {r['subject_id']} ({local_time(r['created_at'])})" for r in batch]
            text = f"{len(batch)} item(s) need moderator review.\n\n" + "\n".join(lines) + f"\n\nOpen the moderator queue (sign-in required):\n{link}\n"
            html = f"<p>{len(batch)} item(s) need moderator review.</p><ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + f'</ul><p><a href="{escape(link, quote=True)}">Open the moderator queue (sign-in required)</a></p>'
            if grievances and not only_grievances:
                complaint_link = root + "&queue=grievances"
                text += f"\nGrievance queue (sign-in required):\n{complaint_link}\n"
                html += f'<p><a href="{escape(complaint_link, quote=True)}">Open the grievance queue (sign-in required)</a></p>'
            for address in addresses:
                await queue_message(conn, dedupe_key=f"alerts:{batch[0]['id']}:{address}",
                                    recipient=address, subject=subject, text=text, html=html, urgent=urgent)
            await conn.execute("UPDATE moderator_alerts SET queued_at=now() WHERE id=ANY($1::uuid[])",
                               [r["id"] for r in batch])
    return len(rows)


REMINDER_SQL = """
    WITH reports AS (
        -- One open episode per sighting, as the queue groups it. The clock
        -- matches moderation_ops.report_clock: the earlier of the ordinary
        -- and severe targets, from the first unanswered report.
        SELECT r.sighting_id AS subject_id, min(r.opened_at) AS started_at,
               LEAST(min(r.opened_at) + $1 * interval '1 hour',
                     min(r.severe_at) + $2 * interval '1 hour') AS due_at,
               'report' AS kind, 'severe' AS clock
        FROM sighting_reports r
        WHERE r.resolved_at IS NULL
        GROUP BY r.sighting_id
        HAVING min(r.severe_at) IS NOT NULL
    ), grievance_clocks AS (
        SELECT g.id AS subject_id, g.received_at AS started_at, g.removal_due_at AS due_at,
               'grievance' AS kind, 'removal' AS clock
        FROM grievances g
        WHERE g.resolved_at IS NULL AND g.removed_at IS NULL AND g.removal_due_at IS NOT NULL
          AND g.kind NOT IN ('other', 'appeal')
        UNION ALL
        SELECT g.id, g.received_at, g.resolve_due_at, 'grievance', 'resolve'
        FROM grievances g
        WHERE g.resolved_at IS NULL AND g.kind NOT IN ('other', 'appeal')
    ), due AS (
        SELECT c.*, CASE WHEN now() >= c.due_at THEN 'overdue' ELSE 'due_soon' END AS stage
        FROM (SELECT * FROM reports UNION ALL SELECT * FROM grievance_clocks) c
        WHERE now() >= c.started_at + (c.due_at - c.started_at) * $3
    ), keyed AS (
        SELECT subject_id, due_at, kind || '_' || clock || '_' || stage AS category,
               format('reminder:%s:%s:%s:%s:%s', kind, subject_id, clock,
                      floor(extract(epoch FROM started_at))::bigint, stage) AS event_key
        FROM due
    )
    INSERT INTO moderator_alerts (id, event_key, category, subject_id, urgent, available_at)
    SELECT gen_random_uuid(), k.event_key, k.category, k.subject_id, true, now()
    FROM keyed k
    WHERE NOT EXISTS (SELECT 1 FROM moderator_alerts a WHERE a.event_key = k.event_key)
    ORDER BY k.due_at
    LIMIT 500
    ON CONFLICT (event_key) DO NOTHING
"""


async def escalate_deadlines(conn) -> int:
    """Queue one urgent reminder near a deadline and one when it passes.

    Covers severe report episodes and urgent (not "other"/"appeal") grievances still
    open. Reuses the alert outbox; stable event keys (per open episode and
    stage) make every check idempotent across workers and restarts. An item
    first seen already overdue gets only the overdue reminder. Grievance
    categories start with grievance_ so the email links the grievance queue.
    Returns the number of reminders queued.
    """
    result = await conn.execute(REMINDER_SQL, float(settings.moderation_response_hours),
                                float(settings.moderation_severe_response_hours),
                                float(settings.moderation_reminder_fraction))
    return int(result.rsplit(" ", 1)[1])


async def deliver_one(pool) -> bool:
    # Console is a development logger, not delivery. Keep mail pending until
    # the real sender is selected, including acknowledgement deadline clocks.
    if settings.email_sender != "ses":
        return False
    token = uuid7()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE moderation_mail SET lease_token=$1, lease_until=now()+$2*interval '1 second', "
            "attempts=attempts+1 WHERE id=(SELECT id FROM moderation_mail "
            "WHERE sent_at IS NULL AND failed_at IS NULL AND available_at<=now() "
            "AND (lease_until IS NULL OR lease_until<=now()) "
            "ORDER BY urgent DESC,available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
            token, float(MAIL_LEASE_S))
    if row is None:
        return False
    try:
        await asyncio.wait_for(get_sender().send_message(row["recipient"], row["subject"],
                                                        row["body_text"], row["body_html"]), MAIL_TIMEOUT_S)
    except Exception as exc:
        # Exception messages can contain provider identifiers or addresses.
        # Store only the class; detailed complaint content never enters logs.
        # Bounded: a permanent provider error (unverified sender, rejected
        # address) must not retry forever. Given-up mail stays visible in the
        # moderator view and /health/detailed until an operator requeues it.
        give_up = row["attempts"] >= settings.moderation_mail_max_attempts
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE moderation_mail SET last_error=$3, lease_until=NULL, lease_token=NULL, "
                "available_at=now()+$4*interval '1 second', failed_at=CASE WHEN $5 THEN now() END "
                "WHERE id=$1 AND lease_token=$2",
                row["id"], token, type(exc).__name__, float(min(3600, 30 * 2 ** min(row["attempts"], 7))), give_up)
        logger.warning("moderation mail delivery failed id=%s error=%s gave_up=%s", row["id"], type(exc).__name__, give_up)
    else:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE moderation_mail SET sent_at=now(), last_error=NULL, "
                               "lease_until=NULL, lease_token=NULL WHERE id=$1 AND lease_token=$2",
                               row["id"], token)
    return True


async def consumer(pool):
    last_escalation = float("-inf")
    loop = asyncio.get_running_loop()
    while True:
        try:
            async with pool.acquire() as conn:
                if loop.time() - last_escalation >= settings.moderation_reminder_check_s:
                    last_escalation = loop.time()
                    await escalate_deadlines(conn)
                await prepare_alerts(conn)
            # Reconsider urgent work after every send, including during a
            # large digest. Network I/O holds no pool connection.
            delivered = await deliver_one(pool)
        except Exception as exc:
            logger.warning("moderation mail consumer failed error=%s", type(exc).__name__)
            delivered = False
        await asyncio.sleep(0 if delivered else settings.moderation_mail_poll_s)
