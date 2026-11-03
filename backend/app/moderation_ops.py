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


def report_clock(first_reported: datetime, first_severe: datetime | None, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    due = first_reported + timedelta(hours=settings.moderation_response_hours)
    if first_severe is not None:
        due = min(due, first_severe + timedelta(hours=settings.moderation_severe_response_hours))
    return {"first_reported_at": first_reported, "age_seconds": max(0, int((now - first_reported).total_seconds())),
            "response_due_at": due, "overdue": now >= due, "severe": first_severe is not None}
