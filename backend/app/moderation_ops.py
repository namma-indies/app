"""The action ledger and provisional response targets, shared by moderation flows."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.ids import uuid7

SEVERE_REASONS = frozenset({"people_privacy", "sexual_content", "illegal_content"})
Action = Literal["hide", "unhide", "ban", "unban", "approve", "reject", "dismiss"]


def action_reason(reason: str) -> str:
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(422, "A decision reason of 1 to 500 characters is required")
    return reason


async def record_action(conn, *, moderator_id: UUID, target_kind: str, target_id: UUID,
                        action: Action, reason: str, previous_state: str | None = None,
                        new_state: str | None = None):
    """Call inside the same transaction as the state change. Never update a row."""
    await conn.execute(
        "INSERT INTO moderation_actions (id, moderator_id, target_kind, target_id, action, "
        "reason, previous_state, new_state) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        uuid7(), moderator_id, target_kind, target_id, action, action_reason(reason),
        previous_state, new_state)


async def apply_sighting_action(conn, *, sighting_id: UUID, verdict: str, moderator_id: UUID,
                                reason: str, action: Action | None = None):
    """Apply to the shared capture and audit each changed sighting.

    The caller owns the transaction, including any associated grievance.
    Capture locking matches reporting/erasure so sibling photos cannot escape.
    """
    capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id=$1", sighting_id)
    await conn.execute("SELECT id FROM captures WHERE id=$1 FOR UPDATE", capture_id)
    old_rows = await conn.fetch(
        "SELECT id, review_status FROM sightings WHERE id=$1 OR capture_id=$2 FOR UPDATE",
        sighting_id, capture_id)
    old_states = {r["id"]: r["review_status"] for r in old_rows}
    if sighting_id not in old_states:
        raise HTTPException(404, "no such sighting")
    ids = list(old_states)
    await conn.execute("UPDATE sightings SET review_status=$2, reviewed_at=now(), reviewed_by=$3, "
                       "updated_at=now() WHERE id=ANY($1::uuid[])", ids, verdict, moderator_id)
    await conn.execute("UPDATE sighting_reports SET resolved_at=now() "
                       "WHERE sighting_id=ANY($1::uuid[]) AND resolved_at IS NULL", ids)
    for sid, old_state in old_states.items():
        audit_action = action or ("hide" if verdict == "rejected" else "unhide" if old_state != "valid" else "dismiss")
        await record_action(conn, moderator_id=moderator_id, target_kind="sighting", target_id=sid,
                            action=audit_action, reason=reason, previous_state=old_state, new_state=verdict)


def report_clock(first_reported: datetime, first_severe: datetime | None, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    due = first_reported + timedelta(hours=settings.moderation_response_hours)
    if first_severe is not None:
        due = min(due, first_severe + timedelta(hours=settings.moderation_severe_response_hours))
    return {"first_reported_at": first_reported, "age_seconds": max(0, int((now - first_reported).total_seconds())),
            "response_due_at": due, "overdue": now >= due, "severe": first_severe is not None}
