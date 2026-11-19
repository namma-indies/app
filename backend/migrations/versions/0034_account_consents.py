"""Versioned terms/guidelines agreement and privacy acknowledgment.

Revision ID: 0034_account_consents
Revises: 0033_blocking
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034_account_consents"
down_revision: Union[str, None] = "0033_blocking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE account_consents (
            observer_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            terms_version text NOT NULL CHECK (length(terms_version) BETWEEN 1 AND 100),
            privacy_version text NOT NULL CHECK (length(privacy_version) BETWEEN 1 AND 100),
            community_guidelines_version text NOT NULL CHECK (length(community_guidelines_version) BETWEEN 1 AND 100),
            minimum_age smallint NOT NULL CHECK (minimum_age BETWEEN 0 AND 120),
            accepted_at timestamptz NOT NULL,
            source text NOT NULL CHECK (source IN ('email', 'passcode', 'account')),
            PRIMARY KEY (observer_id, terms_version, privacy_version, community_guidelines_version, minimum_age)
        );
        GRANT SELECT, INSERT, DELETE ON account_consents TO app_rw;
        ALTER TABLE login_tokens ADD COLUMN signup_consent jsonb
            CHECK (signup_consent IS NULL OR purpose = 'login');
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE login_tokens DROP COLUMN IF EXISTS signup_consent;
        DROP TABLE IF EXISTS account_consents;
    """)
