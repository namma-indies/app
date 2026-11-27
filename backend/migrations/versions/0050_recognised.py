"""A number an animal keeps, and when a sighting was recognised.

Revision ID: 0050_recognised
Revises: 0049_multi_animal_grouping

Akash, 2026-11-27: tell people when a sighting of theirs has been recognised
as an animal ("your sighting from September is No. 047"), with the earlier
photo beside the new one.

* individuals.number: the "NO. 047" on a dog's card. It was the card's
  position in a list sorted by last-seen, worked out in the browser, so it
  changed whenever any dog was seen again and differed between two people
  whose blocks hid different sightings. Nothing can be told about No. 047
  until No. 047 stays No. 047. One sequence for dogs and cats; numbers are
  never reused, so an animal whose sightings are all deleted leaves a gap.
  Existing animals are numbered in the order they were first recognised,
  with merged-away and empty ones last so the visible run has no early gaps.
* sightings.identified_at: when this sighting got its individual. Kept by a
  trigger rather than by each writer: there are three today (a "same" verdict,
  an identical photo, a moderator's unlink) and the next one would forget.
  Cleared when the link is. Existing links are dated from their verdict or
  auto-link where one is on record, otherwise from the row's last update,
  which errs old: a stale date announces nothing.
* observers.recognitions_seen_at: how far this person has read. NULL means
  never; the route treats that as a recent window, not as all of history.

Downgrade drops all of it; the number is display-only and nothing else keys
on it.
"""
from alembic import op

revision = "0050_recognised"
down_revision = "0049_multi_animal_grouping"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE SEQUENCE individuals_number_seq;
        ALTER TABLE individuals ADD COLUMN number integer;
        UPDATE individuals i SET number = n.rn
        FROM (
            SELECT id, row_number() OVER (ORDER BY
                (merged_into IS NOT NULL OR status IS NOT DISTINCT FROM 'merged'),
                (NOT EXISTS (SELECT 1 FROM sightings s WHERE s.individual_id = individuals.id)),
                created_at, id) AS rn
            FROM individuals
        ) n
        WHERE n.id = i.id;
        SELECT setval('individuals_number_seq',
                      COALESCE((SELECT max(number) FROM individuals), 0) + 1, false);
        ALTER TABLE individuals
            ALTER COLUMN number SET DEFAULT nextval('individuals_number_seq'),
            ALTER COLUMN number SET NOT NULL,
            ADD CONSTRAINT individuals_number_key UNIQUE (number);
        -- Owned, so TRUNCATE ... RESTART IDENTITY and a dropped column take it along.
        ALTER SEQUENCE individuals_number_seq OWNED BY individuals.number;
        GRANT USAGE, SELECT ON SEQUENCE individuals_number_seq TO app_rw;

        ALTER TABLE sightings ADD COLUMN identified_at timestamptz;
        UPDATE sightings s SET identified_at = COALESCE(
            (SELECT max(c.created_at) FROM confirmations c
              WHERE c.verdict = 'same' AND c.individual_id = s.individual_id
                AND c.sighting_id = s.id),
            -- A verdict names only the proposal's own sighting; the one it was
            -- matched against is reached through the proposal.
            (SELECT max(c.created_at) FROM confirmations c
               JOIN match_proposals mp ON mp.id = c.proposal_id
              WHERE c.verdict = 'same' AND c.individual_id = s.individual_id
                AND mp.candidate_sighting_id = s.id),
            (SELECT max(a.created_at) FROM auto_links a
              WHERE a.individual_id = s.individual_id
                AND (a.sighting_id = s.id OR a.matched_sighting_id = s.id)),
            s.updated_at)
        WHERE s.individual_id IS NOT NULL;

        CREATE FUNCTION sightings_stamp_identified() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.individual_id IS NOT NULL AND NEW.identified_at IS NULL THEN
                    NEW.identified_at := now();
                END IF;
            ELSIF NEW.individual_id IS DISTINCT FROM OLD.individual_id THEN
                NEW.identified_at := CASE WHEN NEW.individual_id IS NULL THEN NULL ELSE now() END;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER sightings_identified
            BEFORE INSERT OR UPDATE OF individual_id ON sightings
            FOR EACH ROW EXECUTE FUNCTION sightings_stamp_identified();
        -- "What of mine was recognised since I last looked" is asked on every
        -- open of the app; without this it reads every sighting a person has.
        CREATE INDEX ix_sightings_identified ON sightings (observer_id, identified_at)
            WHERE identified_at IS NOT NULL;

        ALTER TABLE observers ADD COLUMN recognitions_seen_at timestamptz;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE observers DROP COLUMN recognitions_seen_at;
        DROP INDEX ix_sightings_identified;
        DROP TRIGGER sightings_identified ON sightings;
        DROP FUNCTION sightings_stamp_identified();
        ALTER TABLE sightings DROP COLUMN identified_at;
        ALTER TABLE individuals DROP COLUMN number;
    """)
