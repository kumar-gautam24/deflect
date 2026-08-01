"""eval runs and per-item results"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("git_sha", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("judge_version", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("retrieval_config", sa.JSON, nullable=False),
        sa.Column("thresholds", sa.JSON, nullable=False),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("metrics", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("escalated", sa.Boolean, nullable=False),
        sa.Column("expected_escalate", sa.Boolean, nullable=False),
        sa.Column("retrieved_sources", sa.JSON, nullable=False),
        sa.Column("hit_at_5", sa.Float, nullable=False),
        sa.Column("mrr", sa.Float, nullable=False),
        sa.Column("faithfulness", sa.Float, nullable=True),
        sa.Column("answer_relevance", sa.Float, nullable=True),
        sa.Column("context_relevance", sa.Float, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
    )
    op.create_index("ix_eval_results_run_id", "eval_results", ["run_id"])


def downgrade() -> None:
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
