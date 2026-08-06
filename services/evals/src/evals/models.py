from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    git_sha: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    judge_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    retrieval_config: Mapped[dict] = mapped_column(JSON)
    thresholds: Mapped[dict] = mapped_column(JSON)
    item_count: Mapped[int] = mapped_column(Integer)
    # running until every item is accounted for, then complete. A run is created the
    # moment it is requested so its progress is observable from the first second.
    status: Mapped[str] = mapped_column(String(16), default="running")
    # What was asked for. item_count stays what was actually scored, so a run that lost
    # items to provider failures says so rather than claiming full coverage.
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvalResult(Base):
    __tablename__ = "eval_results"
    # At-least-once delivery means a worker that wrote its result and died before
    # acknowledging sees the item again. Without this the score is counted twice and the
    # metrics are quietly wrong.
    __table_args__ = (UniqueConstraint("run_id", "item_id", name="eval_results_run_item_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(Boolean)
    expected_escalate: Mapped[bool] = mapped_column(Boolean)
    retrieved_sources: Mapped[list] = mapped_column(JSON)
    hit_at_5: Mapped[float] = mapped_column(Float)
    mrr: Mapped[float] = mapped_column(Float)
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)
    answer_relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalItemJob(Base):
    """One item of one run.

    The completion signal for a run, deliberately not the result row: an item that fails
    permanently never writes a result, and counting results would leave the run stalled
    at 79 of 80 forever, looking like work still in progress.
    """

    __tablename__ = "eval_item_jobs"
    __table_args__ = (UniqueConstraint("run_id", "item_id", name="eval_item_jobs_run_item_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
