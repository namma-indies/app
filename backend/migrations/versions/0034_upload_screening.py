"""Screening an upload before anyone else sees it (#181).

`captures.screening`: where an upload stands with the filter.

    pending   just uploaded, not screened yet: off every shared surface
    held      screening flagged it (`hold_reasons`): waits for a moderator
    clear     screened, nothing flagged: shown as before
    approved  held, and a moderator let it through
    rejected  held, and a moderator kept it off (reversible: approve later)

Only `clear` and `approved` reach anyone but the owner and moderators
(`app.aggregates.screened`). On a capture, not a sighting: a multi-animal
upload is one upload, and its sightings stand or fall together.

Existing uploads become `clear`: everything already public stays public. The
default stays `clear` so rows written by scripts and fixtures behave as
before; the two upload routes write `pending` in the transaction that creates
the capture, so a new upload is never public before it is screened.

`detections.person`, `person_area`, `indoor`: what the detector saw besides
animals, for the screening rules (`app.screening`). NULL is "not measured" (an
older row, or a worker that does not report them yet), not zero.

Revision ID: 0034_upload_screening
Revises: 0033_blocking
Create Date: 2026-11-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034_upload_screening"
down_revision: Union[str, None] = "0033_blocking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE captures
            ADD COLUMN screening text NOT NULL DEFAULT 'clear'
                CHECK (screening IN ('pending','held','clear','approved','rejected')),
            ADD COLUMN hold_reasons text[] NOT NULL DEFAULT '{}',
            ADD COLUMN screened_at timestamptz,
            ADD COLUMN screening_reviewed_at timestamptz,
            ADD COLUMN screening_reviewed_by uuid REFERENCES observers(id) ON DELETE SET NULL;
        -- The moderator queue, and the new-account count, read only these.
        CREATE INDEX ix_captures_screening_open ON captures (created_at, id)
            WHERE screening IN ('pending','held');

        ALTER TABLE detections
            ADD COLUMN person real CHECK (person BETWEEN 0 AND 1),
            ADD COLUMN person_area real CHECK (person_area BETWEEN 0 AND 1),
            ADD COLUMN indoor real CHECK (indoor BETWEEN 0 AND 1);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE detections DROP COLUMN IF EXISTS indoor,
            DROP COLUMN IF EXISTS person_area, DROP COLUMN IF EXISTS person;
        DROP INDEX IF EXISTS ix_captures_screening_open;
        ALTER TABLE captures DROP COLUMN IF EXISTS screening_reviewed_by,
            DROP COLUMN IF EXISTS screening_reviewed_at, DROP COLUMN IF EXISTS screened_at,
            DROP COLUMN IF EXISTS hold_reasons, DROP COLUMN IF EXISTS screening;
        """
    )
