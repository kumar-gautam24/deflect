from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# created_at is set Python-side rather than by a server default so the value is
# readable immediately after flush. Tests run inside a rolled-back transaction and
# never refresh, and a server default would leave the attribute unpopulated there.


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    top_score: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    retrieved: Mapped[list] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer)
    # The gate configuration in force for this request, so a row explains its own
    # escalation without needing to know what the service was configured with.
    min_top_score: Mapped[float] = mapped_column(Float)
    min_margin: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[int] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
