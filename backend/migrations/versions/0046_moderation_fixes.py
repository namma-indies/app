"""Moderation/ops review fixes.

Revision ID: 0046_moderation_fixes
Revises: 0045_match_evidence

* sighting_reports.last_resolved_at remembers that a reporter's earlier report
  was ruled on, after the row is reopened, so the same account cannot re-hide
  an approved capture on its own.
* moderation_actions is append-only for every role, including the table owner
  and the `postgres` login production connects as. REVOKE alone only bound
  app_rw. A superuser can still disable triggers deliberately; this stops
  application code and casual SQL from rewriting history.
* health_write_probe is a one-row table /health/detailed upserts in a
  read-write transaction, so a read-only or full database is detected.
* moderation_mail.failed_at marks a message given up on after a bounded
  number of attempts, instead of retrying a permanent error forever.
"""
from alembic import op

revision = "0046_moderation_fixes"
down_revision = "0045_match_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE sighting_reports ADD COLUMN last_resolved_at timestamptz;
        ALTER TABLE moderation_mail ADD COLUMN failed_at timestamptz;
        CREATE TABLE health_write_probe (
            id smallint PRIMARY KEY CHECK (id = 1),
            touched_at timestamptz NOT NULL
        );
        GRANT SELECT, INSERT, UPDATE ON health_write_probe TO app_rw;

        CREATE FUNCTION moderation_actions_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'moderation_actions is append-only (% refused)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$;
        CREATE TRIGGER moderation_actions_no_update_delete
            BEFORE UPDATE OR DELETE ON moderation_actions
            FOR EACH ROW EXECUTE FUNCTION moderation_actions_append_only();
        CREATE TRIGGER moderation_actions_no_truncate
            BEFORE TRUNCATE ON moderation_actions
            FOR EACH STATEMENT EXECUTE FUNCTION moderation_actions_append_only();
    """)


def downgrade():
    # Removes only the guard and the bookkeeping column; ledger rows are kept.
    op.execute("""
        DROP TRIGGER moderation_actions_no_truncate ON moderation_actions;
        DROP TRIGGER moderation_actions_no_update_delete ON moderation_actions;
        DROP FUNCTION moderation_actions_append_only();
        ALTER TABLE sighting_reports DROP COLUMN last_resolved_at;
        ALTER TABLE moderation_mail DROP COLUMN failed_at;
        DROP TABLE health_write_probe;
    """)
