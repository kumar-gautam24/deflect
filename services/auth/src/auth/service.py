"""Login, logout, and the decisions around them.

`now` is a parameter rather than a call to the clock, so lockout windows are tested
without sleeping.
"""

import secrets
from datetime import datetime, timedelta

from deflect_common.auth import hash_token
from deflect_common.sessions import SessionStore
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import AdminUser, Session
from auth.passwords import hash_password, needs_rehash, verify_password
from auth.policy import Policy

# A dummy hash to verify against when no account exists, so an unknown address costs the
# same work as a known one and cannot be distinguished by response time.
_ABSENT_USER_HASH = hash_password(secrets.token_urlsafe(16))


class LoginFailed(Exception):
    """Wrong password, or no such account. Deliberately one exception for both."""


class AccountLocked(Exception):
    """Raised only for an account that exists, which is why the route must not say so.

    The user id rides along so the route can log which account was locked. It is the one
    identifier that may appear in a log -- an email there would put the very fact the
    reply withholds into a file that is easier to read than the API.
    """

    def __init__(self, seconds_remaining: int, user_id: int) -> None:
        super().__init__(f"locked for {seconds_remaining} more seconds")
        self.seconds_remaining = seconds_remaining
        self.user_id = user_id


async def login(
    session: AsyncSession,
    sessions: SessionStore,
    email: str,
    password: str,
    now: datetime,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, Session]:
    """Authenticate and issue a session. Returns the raw token and its row.

    The raw token is returned exactly once, here. Nothing stores it.
    """
    user = (
        await session.execute(
            select(AdminUser).where(func.lower(AdminUser.email) == email.lower())
        )
    ).scalar_one_or_none()

    if user is not None and user.locked_until and user.locked_until > now:
        raise AccountLocked(int((user.locked_until - now).total_seconds()), user.id)

    # The hash runs whether or not the account exists. Skipping it for an unknown address
    # would make the response measurably faster and turn login into an account oracle.
    stored = user.password_hash if user is not None else _ABSENT_USER_HASH
    if not verify_password(password, stored) or user is None:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= Policy.LOCK_AFTER_FAILURES:
                user.locked_until = now + timedelta(seconds=Policy.LOCK_SECONDS)
            await session.flush()
        raise LoginFailed

    if needs_rehash(user.password_hash):
        # Migrated on the way past, so a password strengthens without anyone resetting it.
        user.password_hash = hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None

    token = secrets.token_urlsafe(32)
    row = Session(
        token_hash=hash_token(token),
        user_id=user.id,
        role=user.role,
        issued_at=now,
        expires_at=now + timedelta(hours=Policy.SESSION_HOURS),
        ip=ip,
        user_agent=(user_agent or "")[:512] or None,
    )
    session.add(row)
    await session.flush()

    # The cache entry lasts exactly as long as the session it stands for. Every other
    # service resolves a session from the cache alone, so a shorter TTL here would not
    # bound revocation -- it would end the session, while the cookie and the row above
    # both still claimed twelve hours.
    await sessions.put(
        row.token_hash,
        str(user.id),
        user.role,
        ttl_seconds=int((row.expires_at - now).total_seconds()),
    )
    return token, row


async def logout(
    session: AsyncSession, sessions: SessionStore, token_hash: str, now: datetime
) -> None:
    row = (
        await session.execute(select(Session).where(Session.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = now
        await session.flush()

    # The cache delete is what makes revocation immediate. If it fails, the entry expires
    # on its own, which is why the TTL is minutes.
    await sessions.delete(token_hash)


async def logout_all(
    session: AsyncSession, sessions: SessionStore, user_id: int, now: datetime
) -> None:
    rows = (
        await session.execute(
            select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = now
    await session.flush()

    await sessions.delete_user(str(user_id))
