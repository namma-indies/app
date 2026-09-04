"""Give `review_status` a writer, and record who asked for what.

`sightings.review_status` has existed since 0001 with `pending`/`valid`/
`rejected`, and `/map` filtered on it from the day it was written. Nothing has
ever set it to anything but `valid`. The filter is unreachable code: there is
no path in the app, the API, or any script that hides a sighting, so a photo
that should not be on a shared map stays on it.

That is the whole of Apple's Guideline 1.2 requirement for a report mechanism
and Google Play's UGC policy, but it matters here before either store does. The
map shows where free-roaming dogs are. If a sighting endangers an animal --
because of what it shows, or because it was logged by someone acting in bad
faith -- there is currently no way for anyone to take it down short of SQL.

`sighting_reports` is the writer's audit trail. The status could have been
flipped by an endpoint with no table behind it, but then "why is this hidden"
would have no answer, a moderator would be deciding without the reason, and an
observer whose sighting was hidden could not be told what was said about it.

One report per person per sighting, enforced by the primary key rather than
checked in Python: the report endpoint is idempotent by construction, so a
double tap on a phone with a slow connection cannot inflate a count that a
moderator reads as "several people are worried about this".

Revision ID: 0010_sighting_reports
Revises: 0009_sighting_client_token
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_sighting_reports"
down_revision: Union[str, None] = "0009_sighting_client_token"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sighting_reports (
            sighting_id uuid NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
            reporter_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            reason text NOT NULL
                CHECK (reason IN ('endangers_dog','not_a_dog','wrong_place',
                                  'offensive','other')),
            note text,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (sighting_id, reporter_id)
        );
        """
    )
    # When a moderator last ruled, and who. Without this there is no way to
    # tell "valid, nobody has looked" from "valid, a human looked and said so"
    # -- and the report path needs that distinction, or the next person to tap
    # report silently overturns the decision and the last tap wins.
    op.execute("ALTER TABLE sightings ADD COLUMN reviewed_at timestamptz;")
    op.execute(
        "ALTER TABLE sightings ADD COLUMN reviewed_by uuid REFERENCES observers(id);"
    )

    # The moderation queue reads newest-first across all sightings, which is
    # not the primary key's order.
    op.execute(
        "CREATE INDEX ix_sighting_reports_created_at "
        "ON sighting_reports (created_at DESC);"
    )
    # Every display surface now filters `review_status = 'valid'`, so this is
    # on the hot path of /map, /dogs and /proposals. Partial, because the
    # queries ask for one value and the overwhelming majority of rows have it.
    op.execute(
        "CREATE INDEX ix_sightings_under_review ON sightings (review_status) "
        "WHERE review_status <> 'valid';"
    )
    # `trust_tier` has been on observers since 0001 and nothing has ever read
    # or written it. It is the moderator flag -- no new column, and the name
    # already meant this. Stated here because a reader looking for how
    # moderation is authorised will grep the migrations first.
    #
    #   UPDATE observers SET trust_tier = 'moderator' WHERE email = '...';


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_under_review;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS reviewed_by;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS reviewed_at;")
    op.execute("DROP INDEX IF EXISTS ix_sighting_reports_created_at;")
    op.execute("DROP TABLE IF EXISTS sighting_reports;")
    # Deliberately does NOT reset review_status. Going backwards should not
    # silently republish something a human decided to hide.
