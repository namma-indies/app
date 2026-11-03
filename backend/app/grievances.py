"""Intake validation and provisional clocks. Classification needs legal review."""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.auth.allowlist import normalize_email
from app.config import settings

GrievanceKind = Literal["takedown", "privacy", "intimate_imagery", "government_order", "other"]
KIND_LABELS = {"takedown": "Takedown", "privacy": "Privacy",
               "intimate_imagery": "Non-consensual intimate imagery",
               "government_order": "Government or court order", "other": "Other"}
LEGAL_NOTE = "Provisional tracking targets: confirm every deadline and its applicability with a lawyer."


def removal_hours(kind: str) -> float | None:
    if kind == "intimate_imagery":
        return settings.grievance_intimate_hours
    if kind == "government_order":
        return settings.grievance_government_hours
    if kind in {"takedown", "privacy"}:
        return settings.grievance_removal_hours
    return None


def linked_sighting(target: str) -> UUID | None:
    try:
        return UUID(target)
    except ValueError:
        parsed = urlsplit(target)
        own = urlsplit(settings.public_base_url)
        if parsed.netloc == own.netloc and parsed.scheme in {"http", "https"}:
            match = re.fullmatch(r"/sighting/([0-9a-fA-F-]{36})/?", parsed.path)
            if match:
                try:
                    return UUID(match[1])
                except ValueError:
                    pass
    return None


class Intake(BaseModel):
    submission_key: UUID
    kind: GrievanceKind
    target: str = Field(min_length=1, max_length=1000)
    details: str = Field(min_length=1, max_length=5000)
    contact_email: str = Field(min_length=1, max_length=254)

    @field_validator("target", "details", "contact_email", mode="before")
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("contact_email")
    @classmethod
    def email(cls, value):
        value = normalize_email(value)
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value):
            raise ValueError("Enter a valid contact email")
        return value

    @field_validator("target")
    @classmethod
    def url_or_id(cls, value):
        try:
            UUID(value)
            return value
        except ValueError:
            try:
                parsed = urlsplit(value)
                if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                    return value
            except ValueError:
                pass
        raise ValueError("Enter a link or sighting ID")

    def digest(self) -> str:
        values = self.model_dump(mode="json", exclude={"submission_key"})
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def clocks(row, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    unresolved = row["resolved_at"] is None
    ack = row["acknowledged_at"]
    due = []
    if ack is None:
        due.append(row["acknowledge_due_at"])
    if unresolved:
        due.append(row["resolve_due_at"])
        if row["removal_due_at"] and row["removed_at"] is None:
            due.append(row["removal_due_at"])
    return {"age_seconds": max(0, int((now - row["received_at"]).total_seconds())),
            "acknowledgement_overdue": ack is None and now >= row["acknowledge_due_at"],
            "resolution_overdue": unresolved and now >= row["resolve_due_at"],
            "removal_overdue": bool(unresolved and row["removed_at"] is None and row["removal_due_at"] and now >= row["removal_due_at"]),
            "next_due_at": min(due) if due else None,
            "legal_confirmation_required": True}
