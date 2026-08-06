from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    # Persisted rather than held in memory, so a restart does not clear a lockout -- the
    # same reasoning that put the ask limiter's daily cap in the database.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # Declared here as well as in migration 0001 so that `alembic revision --autogenerate`
    # sees it as already present. Left off the model, autogenerate proposes dropping it --
    # and this index is the only thing enforcing case-insensitive uniqueness, since the
    # UNIQUE on email above is case-sensitive. Losing it would silently let You@x.com and
    # you@x.com both exist, with one of them unable to log in reliably.
    __table_args__ = (
        Index("admin_users_email_lower_idx", text("lower(email)"), unique=True),
    )


class Session(Base):
    """A live login.

    Only the SHA-256 of the token is stored, so a dump of this table yields nothing that
    can be replayed. The role is denormalised here so a validating service needs one Redis
    read and no join; changing a user's role therefore takes effect at their next login,
    not retroactively.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # "every live session for this user" -- the logout-everywhere query. Declared for the
    # same reason as the index above: to keep autogenerate from proposing its removal.
    __table_args__ = (
        Index(
            "sessions_user_live_idx",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
