"""individual_names: one row per naming event, nothing ever overwritten

Identity has had an event log since 0001 -- `match_proposals` and
`confirmations` record every claim that two sightings are one dog, and no
verdict is destroyed. Naming has had three columns on `individuals`: `name`,
`named_by`, `named_at`. That is a single, destructive, last-writer-wins name,
which is precisely the failure issue #4 is written to prevent: a dog already
called Kaju by the woman who has fed him for three years renamed Bruno by
someone who walked past once, with no history and no way back.

This table is the append-only half of the fix. A name is an event: proposed by
someone, possibly resolved by someone else, never deleted. A bad rename
becomes a one-click revert rather than a loss.

**Nothing reads or writes this table yet.** It is here now because the
migration is free while no names exist and expensive once they do -- #4 and
#55 both identify it as the one naming decision with a closing window. Who may
name a dog (#4), what a name even is when a dog has three (#55), and what
earns the standing to decide (#56) are all deliberately unbuilt.

`individuals.name` / `named_by` / `named_at` stay where they are and become a
*cache* of the active row -- the same caches-over-an-event-log pattern as
`sightings.match_status` over `confirmations`. Reads stay cheap, the log stays
authoritative. No code changes, because no code writes names today.

Two choices worth stating:

`proposed_by` is nullable, answering #4's open question. A name can arrive
from a model suggestion or a WhatsApp intake with no account behind it, and
NOT NULL would force a fake observer row for every such name.

The partial unique index is the real invariant: at most one `active` name per
individual, which is exactly what the cache column can represent. The database
refuses the inconsistent state rather than trusting application code that has
not been written yet to avoid it.

Revision ID: 0012_individual_names
Revises: 0011_areas_kind
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012_individual_names"
down_revision: Union[str, None] = "0011_areas_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE individual_names (
            id uuid PRIMARY KEY,
            individual_id uuid NOT NULL REFERENCES individuals(id),
            name text NOT NULL,
            proposed_by uuid REFERENCES observers(id),
            status text NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','active','superseded','rejected')),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_by uuid REFERENCES observers(id),
            resolved_at timestamptz
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_individual_names_individual_id "
        "ON individual_names (individual_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_individual_names_active "
        "ON individual_names (individual_id) WHERE status = 'active';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS individual_names;")
