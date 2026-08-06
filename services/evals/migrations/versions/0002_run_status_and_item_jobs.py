"""run status, item totals and per-item jobs

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing runs finished under the synchronous runner, so they are complete by
    # definition and their item_count is also the target they were asked for.
    op.add_column(
        "eval_runs",
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
    )
    op.add_column(
        "eval_runs",
        sa.Column("items_total", sa.Integer, nullable=False, server_default="0"),
    )
    op.execute("UPDATE eval_runs SET items_total = item_count")

    # The server defaults existed only to make the columns NOT NULL while backfilling.
    # Left in place, status would default to 'complete' at the database level while the
    # model defaults to 'running', so any insert bypassing the ORM -- raw SQL, Core
    # insert(), a future admin script -- would silently create a run that claims to be
    # finished before it starts.
    op.alter_column("eval_runs", "status", server_default=None)
    op.alter_column("eval_runs", "items_total", server_default=None)

    # A duplicate pair would make the constraint fail to build. None exist in the
    # development database -- the old runner wrote each item once -- but a migration that
    # dies on real data somewhere else is worse than one that says what it removed.
    op.execute(
        """
        DELETE FROM eval_results a
        USING eval_results b
        WHERE a.run_id = b.run_id AND a.item_id = b.item_id AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        "eval_results_run_item_key", "eval_results", ["run_id", "item_id"]
    )

    op.create_table(
        "eval_item_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "item_id", name="eval_item_jobs_run_item_key"),
    )
    # The fan-in query, run once per finished item: how many of this run are done.
    op.create_index("eval_item_jobs_run_status_idx", "eval_item_jobs", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("eval_item_jobs_run_status_idx", "eval_item_jobs")
    op.drop_table("eval_item_jobs")
    op.drop_constraint("eval_results_run_item_key", "eval_results", type_="unique")
    op.drop_column("eval_runs", "items_total")
    op.drop_column("eval_runs", "status")
