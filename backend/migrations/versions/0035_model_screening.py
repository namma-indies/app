"""The local model as a second screening signal (#181, app/model_screening.py).

`captures.screening = 'checking'`: the detector found nothing to hold, and the
local model has been asked to look too. Hidden while it looks, for at most
`screening_model_wait_s` from `screened_at`; after that it shows on the
detector's verdict alone, so a model that is down never blanks the map. The
model's answer then clears it or holds it, even if it is showing by then.

`upload_screens`: one model look per upload, leased to the worker like a photo
description.

Revision ID: 0035_model_screening
Revises: 0034_upload_screening
Create Date: 2026-11-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0035_model_screening"
down_revision: Union[str, None] = "0034_upload_screening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE captures DROP CONSTRAINT captures_screening_check,
            ADD CONSTRAINT captures_screening_check
                CHECK (screening IN ('pending','checking','held','clear','approved','rejected'));

        CREATE TABLE upload_screens (
            id uuid PRIMARY KEY,
            capture_id uuid NOT NULL UNIQUE REFERENCES captures(id) ON DELETE CASCADE,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
            attempts int NOT NULL DEFAULT 0,
            run_after timestamptz NOT NULL DEFAULT now(),
            lease_until timestamptz,
            lease_token_hash text,
            error text,
            -- What the model flagged, as hold reasons; empty when it saw nothing.
            flags text[],
            model text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_upload_screens_queue ON upload_screens (created_at, id)
            WHERE status IN ('pending', 'running');
        GRANT SELECT, INSERT, UPDATE, DELETE ON upload_screens TO app_rw;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS upload_screens;
        UPDATE captures SET screening = 'clear' WHERE screening = 'checking';
        ALTER TABLE captures DROP CONSTRAINT captures_screening_check,
            ADD CONSTRAINT captures_screening_check
                CHECK (screening IN ('pending','held','clear','approved','rejected'));
        """
    )
