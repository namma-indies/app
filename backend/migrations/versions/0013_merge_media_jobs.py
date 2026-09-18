"""Join main and GPU histories without rewriting revisions already on staging."""

revision = "0013_merge_media_jobs"
down_revision = ("0012_individual_names", "0011_media_jobs")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
