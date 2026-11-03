"""Durable moderator alerts and mail delivery through the existing sender.

`alert_moderators` is the integration seam for reports, upload screening and
grievances. Enqueue it in the transaction that creates the work to be reviewed.
Delivery is at least once: a crash after the provider accepts mail can duplicate
that one message, but cannot lose the event. No evidence or contact data belongs
in an alert; moderators follow the authenticated queue link.
"""
import asyncio
import logging
from html import escape
from uuid import UUID

from app.config import settings
from app.email.sender import get_sender
from app.ids import uuid7

logger = logging.getLogger(__name__)
MAIL_TIMEOUT_S = 20
MAIL_LEASE_S = 90


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
            link = settings.public_base_url.rstrip("/") + "/?tab=moderation"
            # Counts, machine labels and opaque IDs only: never forward reports,
            # images, precise locations or complainant addresses by email.
            lines = [f"{r['category']}: {r['subject_id']} ({r['created_at'].isoformat()})" for r in batch]
            text = f"{len(batch)} item(s) need moderator review.\n\n" + "\n".join(lines) + f"\n\nOpen the moderator queue (sign-in required):\n{link}\n"
            html = f"<p>{len(batch)} item(s) need moderator review.</p><ul>" + "".join(f"<li>{escape(line)}</li>" for line in lines) + f'</ul><p><a href="{escape(link, quote=True)}">Open the moderator queue (sign-in required)</a></p>'
            for address in addresses:
                await queue_message(conn, dedupe_key=f"alerts:{batch[0]['id']}:{address}",
                                    recipient=address, subject=subject, text=text, html=html, urgent=urgent)
            await conn.execute("UPDATE moderator_alerts SET queued_at=now() WHERE id=ANY($1::uuid[])",
                               [r["id"] for r in batch])
    return len(rows)


async def deliver_one(pool) -> bool:
    token = uuid7()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE moderation_mail SET lease_token=$1, lease_until=now()+$2*interval '1 second', "
            "attempts=attempts+1 WHERE id=(SELECT id FROM moderation_mail "
            "WHERE sent_at IS NULL AND available_at<=now() "
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
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE moderation_mail SET last_error=$3, lease_until=NULL, lease_token=NULL, "
                "available_at=now()+$4*interval '1 second' WHERE id=$1 AND lease_token=$2",
                row["id"], token, type(exc).__name__, float(min(3600, 30 * 2 ** min(row["attempts"], 7))))
        logger.warning("moderation mail delivery failed id=%s error=%s", row["id"], type(exc).__name__)
    else:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE moderation_mail SET sent_at=now(), last_error=NULL, "
                               "lease_until=NULL, lease_token=NULL WHERE id=$1 AND lease_token=$2",
                               row["id"], token)
    return True


async def consumer(pool):
    while True:
        try:
            async with pool.acquire() as conn:
                await prepare_alerts(conn)
            # Reconsider urgent work after every send, including during a
            # large digest. Network I/O holds no pool connection.
            delivered = await deliver_one(pool)
        except Exception as exc:
            logger.warning("moderation mail consumer failed error=%s", type(exc).__name__)
            delivered = False
        await asyncio.sleep(0 if delivered else settings.moderation_mail_poll_s)
