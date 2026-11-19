"""Versioned agreement receipts; public legal text is maintained separately."""
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import Form, HTTPException
from pydantic import BaseModel, Field

from app.auth.email_login import get_or_create_observer_by_email
from app.auth.magiclink import create_observer
from app.config import settings

CONSENT_REQUIRED = "consent_required"


class ConsentSubmission(BaseModel):
    terms_version: str = Field(default="", max_length=100)
    privacy_version: str = Field(default="", max_length=100)
    community_guidelines_version: str = Field(default="", max_length=100)
    agree_terms_and_guidelines: bool = False
    acknowledge_privacy: bool = False
    minimum_age: int | None = Field(default=None, ge=1, le=120)
    age_confirmed: bool = False


class ConsentSnapshot(BaseModel):
    terms_version: str = Field(min_length=1, max_length=100)
    privacy_version: str = Field(min_length=1, max_length=100)
    community_guidelines_version: str = Field(min_length=1, max_length=100)
    minimum_age: int = Field(ge=0, le=120)
    accepted_at: datetime


class ConsentRequired(HTTPException):
    def __init__(self, message: str = "Please agree to the Terms of Service and Community Guidelines, and acknowledge the Privacy Policy."):
        super().__init__(428, message, headers={"Cache-Control": "no-store"})


def current_policy() -> dict:
    return {
        "enabled": settings.consent_enabled,
        "terms_version": settings.terms_version,
        "privacy_version": settings.privacy_version,
        "community_guidelines_version": settings.community_guidelines_version,
        "minimum_age": settings.consent_minimum_age,
    }


def validate_submission(submission: ConsentSubmission) -> ConsentSnapshot | None:
    if not settings.consent_enabled:
        return None
    if not submission.agree_terms_and_guidelines or not submission.acknowledge_privacy:
        raise ConsentRequired()
    if (submission.terms_version != settings.terms_version
            or submission.privacy_version != settings.privacy_version
            or submission.community_guidelines_version != settings.community_guidelines_version
            or submission.minimum_age != settings.consent_minimum_age):
        raise ConsentRequired("The policies changed. Please review the current versions and confirm again.")
    if settings.consent_minimum_age is not None and not submission.age_confirmed:
        raise ConsentRequired(f"Please confirm that you are at least {settings.consent_minimum_age} years old.")
    return ConsentSnapshot(
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        community_guidelines_version=settings.community_guidelines_version,
        minimum_age=settings.consent_minimum_age or 0,
        accepted_at=datetime.now(timezone.utc),
    )


async def consent_form(
    terms_version: Annotated[str, Form(max_length=100)] = "",
    privacy_version: Annotated[str, Form(max_length=100)] = "",
    community_guidelines_version: Annotated[str, Form(max_length=100)] = "",
    agree_terms_and_guidelines: Annotated[bool, Form()] = False,
    acknowledge_privacy: Annotated[bool, Form()] = False,
    minimum_age: Annotated[int | None, Form(ge=1, le=120)] = None,
    age_confirmed: Annotated[bool, Form()] = False,
) -> ConsentSubmission:
    return ConsentSubmission(
        terms_version=terms_version, privacy_version=privacy_version,
        community_guidelines_version=community_guidelines_version,
        agree_terms_and_guidelines=agree_terms_and_guidelines,
        acknowledge_privacy=acknowledge_privacy,
        minimum_age=minimum_age, age_confirmed=age_confirmed,
    )


async def record_snapshot(conn, observer_id: UUID, snapshot: ConsentSnapshot | None, *, source: str) -> None:
    if snapshot is None:
        return
    # Serialize with account erasure and identity carry-over. No receipt may
    # appear after the account has been scrubbed into a tombstone.
    row = await conn.fetchrow("SELECT deleted_at FROM observers WHERE id=$1 FOR UPDATE", observer_id)
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(401)
    await conn.execute(
        """INSERT INTO account_consents
           (observer_id, terms_version, privacy_version, community_guidelines_version,
            minimum_age, accepted_at, source)
           VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING""",
        observer_id, snapshot.terms_version, snapshot.privacy_version,
        snapshot.community_guidelines_version, snapshot.minimum_age, snapshot.accepted_at, source,
    )


async def current_acceptance(conn, observer_id: UUID) -> datetime | None:
    if not settings.consent_enabled:
        return None
    return await conn.fetchval(
        """SELECT accepted_at FROM account_consents
           WHERE observer_id=$1 AND terms_version=$2 AND privacy_version=$3
             AND community_guidelines_version=$4 AND minimum_age >= $5
           ORDER BY accepted_at LIMIT 1""",
        observer_id, settings.terms_version, settings.privacy_version,
        settings.community_guidelines_version, settings.consent_minimum_age or 0,
    )


async def needs_consent(conn, observer_id: UUID) -> bool:
    return settings.consent_enabled and await current_acceptance(conn, observer_id) is None


async def passcode_account(conn, display_name: str, submission: ConsentSubmission) -> UUID:
    snapshot = validate_submission(submission)
    async with conn.transaction():
        observer_id = await create_observer(conn, display_name=display_name, created_via="passcode")
        await record_snapshot(conn, observer_id, snapshot, source="passcode")
    return observer_id


async def email_account(conn, email: str, submission: ConsentSubmission, *, creating: bool) -> tuple[UUID | None, ConsentSnapshot | None]:
    # Sign-in and creation are explicit choices. A normal sign-in never
    # creates an account, nor updates an existing person's consent before
    # verification. Unknown addresses get the same neutral sign-in response.
    snapshot = validate_submission(submission) if creating else None
    existing = await conn.fetchval("SELECT id FROM observers WHERE email=$1", email)
    if existing is not None:
        return existing, None
    if settings.consent_enabled and not creating:
        return None, None
    observer_id = await get_or_create_observer_by_email(conn, email=email)
    return observer_id, snapshot


async def consume_email_token(conn, token: str) -> UUID | None:
    from app.auth.logintoken import consume_for_signin

    # Redemption and receipt commit together. A later login token does not
    # inherit the pending consent attached to an earlier, unused token.
    async with conn.transaction():
        row = await consume_for_signin(conn, token)
        if row is None:
            return None
        if row["signup_consent"] is not None:
            snapshot = ConsentSnapshot.model_validate_json(row["signup_consent"])
            await record_snapshot(conn, row["observer_id"], snapshot, source="email")
        return row["observer_id"]
