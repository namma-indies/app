"""Durable moderation alerts, open-report clocks and an action ledger (#198).

Revision ID: 0034_moderation_ops
Revises: 0033_blocking
"""
from alembic import op

revision = "0034_moderation_ops"
down_revision = "0033_blocking"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE sighting_reports DROP CONSTRAINT sighting_reports_reason_check;
        ALTER TABLE sighting_reports ADD CONSTRAINT sighting_reports_reason_check CHECK (
            reason IN ('endangers_dog', 'not_a_dog', 'wrong_place', 'offensive', 'other',
                       'people_privacy', 'sexual_content', 'illegal_content'));
        ALTER TABLE sighting_reports ADD COLUMN opened_at timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN resolved_at timestamptz, ADD COLUMN severe_at timestamptz;
        UPDATE sighting_reports r SET opened_at = r.created_at,
            resolved_at = CASE WHEN r.created_at <= s.reviewed_at THEN s.reviewed_at END
            FROM sightings s WHERE s.id = r.sighting_id;
        CREATE INDEX ix_reports_open ON sighting_reports (opened_at) WHERE resolved_at IS NULL;

        -- IDs deliberately have no foreign keys: erasure must not erase the
        -- record of a moderator's action. No names or email snapshots here.
        CREATE TABLE moderation_actions (
            id uuid PRIMARY KEY,
            moderator_id uuid NOT NULL,
            target_kind text NOT NULL CHECK (target_kind IN ('sighting', 'observer', 'capture', 'grievance')),
            target_id uuid NOT NULL,
            action text NOT NULL CHECK (action IN ('hide', 'unhide', 'ban', 'unban', 'approve', 'reject', 'dismiss')),
            reason text NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
            previous_state text,
            new_state text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_moderation_actions_time ON moderation_actions (created_at DESC, id DESC);
        CREATE INDEX ix_moderation_actions_target ON moderation_actions (target_kind, target_id);
        GRANT SELECT, INSERT ON moderation_actions TO app_rw;
        REVOKE UPDATE, DELETE, TRUNCATE ON moderation_actions FROM app_rw;

        CREATE TABLE moderator_alerts (
            id uuid PRIMARY KEY,
            event_key text NOT NULL UNIQUE CHECK (length(event_key) BETWEEN 1 AND 200),
            category text NOT NULL CHECK (length(category) BETWEEN 1 AND 80),
            subject_id uuid NOT NULL,
            urgent boolean NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            available_at timestamptz NOT NULL,
            queued_at timestamptz
        );
        CREATE INDEX ix_moderator_alerts_due ON moderator_alerts (available_at) WHERE queued_at IS NULL;
        GRANT SELECT, INSERT, UPDATE, DELETE ON moderator_alerts TO app_rw;

        -- One row per recipient. A failed recipient never rolls back mail
        -- already sent to someone else. Leases recover after process death.
        CREATE TABLE moderation_mail (
            id uuid PRIMARY KEY,
            dedupe_key text NOT NULL UNIQUE,
            recipient text NOT NULL,
            subject text NOT NULL,
            body_text text NOT NULL,
            body_html text NOT NULL,
            urgent boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            available_at timestamptz NOT NULL DEFAULT now(),
            lease_token uuid,
            lease_until timestamptz,
            attempts integer NOT NULL DEFAULT 0,
            last_error text,
            sent_at timestamptz
        );
        CREATE INDEX ix_moderation_mail_due ON moderation_mail (available_at) WHERE sent_at IS NULL;
        GRANT SELECT, INSERT, UPDATE, DELETE ON moderation_mail TO app_rw;
    """)


def downgrade():
    op.execute("""
        DROP TABLE moderation_mail;
        DROP TABLE moderator_alerts;
        DROP TABLE moderation_actions;
        ALTER TABLE sighting_reports DROP COLUMN opened_at, DROP COLUMN resolved_at, DROP COLUMN severe_at;
        UPDATE sighting_reports SET reason = 'other'
            WHERE reason IN ('people_privacy', 'sexual_content', 'illegal_content');
        ALTER TABLE sighting_reports DROP CONSTRAINT sighting_reports_reason_check;
        ALTER TABLE sighting_reports ADD CONSTRAINT sighting_reports_reason_check CHECK (
            reason IN ('endangers_dog', 'not_a_dog', 'wrong_place', 'offensive', 'other'));
    """)
