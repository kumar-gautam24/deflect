"""ingest jobs

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("root", sa.String(1024), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chunks", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The worker's only query: find work that is not finished.
    op.create_index("ingest_jobs_status_idx", "ingest_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ingest_jobs_status_idx", "ingest_jobs")
    op.drop_table("ingest_jobs")
