"""Give a capture an identity of its own, so a retry cannot duplicate it.

`POST /sighting` had no idempotency of any kind. The offline queue posts, waits
for the response, and only then deletes the item:

    await postSighting(item);
    await db.delete(STORE, stored.id);

So when the request reaches the server and succeeds but the *response* does not
come back -- signal lost mid-upload, phone asleep, tab closed -- the item stays
`pending` and the next flush posts it again. The server has no way to tell that
from a genuine second capture, so it creates a second sighting. This is a field
app on mobile data; a dropped connection during an upload is ordinary.

The same gap covers a second route in: the queue's in-flight guard is a
module-level variable, so an installed PWA and a browser tab -- two JS contexts
over one shared IndexedDB -- each run their own flush across the same rows.

`client_token` is minted once when the capture is queued and resent on every
attempt, so all attempts at one capture carry the same value and the unique
index collapses them.

Deliberately NOT deduplicated on `phash`. Identical bytes are not the same
event: a person may legitimately log the same dog twice, and two frames of one
clip share almost everything. What must be caught is "this is a retry of that
request", which is a property of the request, not of the pixels.

Unique per observer, not globally. Idempotency is a property of one caller
retrying: scoping the index to `(observer_id, client_token)` is what makes the
uniqueness agree with the lookup, which is also scoped by observer so a guessed
or replayed token cannot hand back somebody else's sighting. A global index with
an observer-scoped lookup disagrees with itself -- the second observer passes
the pre-check, violates the index, and the recovery path cannot find the row it
collided with, so the request 500s.

Partial rather than a plain UNIQUE: every row predating this has NULL, and while
NULLs are distinct in a Postgres unique index anyway, the predicate states the
intent instead of relying on that.

Revision ID: 0009_sighting_client_token
Revises: 0008_clip_and_sighting_vector
Create Date: 2026-09-01

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009_sighting_client_token"
down_revision: Union[str, None] = "0008_clip_and_sighting_vector"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE sightings ADD COLUMN client_token text;")
    op.execute(
        "CREATE UNIQUE INDEX ix_sightings_client_token "
        "ON sightings (observer_id, client_token) "
        "WHERE client_token IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_client_token;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS client_token;")
