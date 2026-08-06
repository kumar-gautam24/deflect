import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import deflect_common.sessions as sessions_module
import pytest
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore

from auth.models import AdminUser
from auth.passwords import hash_password
from auth.policy import Policy
from auth.service import AccountLocked, LoginFailed, login, logout, logout_all

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


async def _user(session, email="a@x.com", password="pw", role="admin") -> AdminUser:
    user = AdminUser(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.flush()
    return user


async def test_a_correct_password_issues_a_session(session):
    await _user(session)
    store = FakeSessionStore()

    token, row = await login(session, store, "a@x.com", "pw", now=NOW)

    assert row.expires_at == NOW + timedelta(hours=Policy.SESSION_HOURS)
    assert await store.get(hash_token(token)) == (str(row.user_id), "admin")


async def test_the_cache_entry_lasts_as_long_as_the_session(session, monkeypatch):
    """Regression: the entry was written with a fixed 300s TTL while both the session row
    and the browser cookie said twelve hours.

    Every service but auth resolves a session from this store alone, so its TTL *is* the
    session length -- a shorter one logged an admin out of /traces five minutes after
    logging in, forever. No test expressed the relationship between the two lifetimes,
    which is why three task-scoped reviews passed over it.
    """
    await _user(session)
    store = FakeSessionStore()

    token, _ = await login(session, store, "a@x.com", "pw", now=NOW)
    start = time.monotonic()

    # An hour in: long past any cache TTL, well short of the session's own expiry.
    monkeypatch.setattr(
        sessions_module, "time", SimpleNamespace(monotonic=lambda: start + 3600)
    )
    assert await store.get(hash_token(token)) is not None

    monkeypatch.setattr(
        sessions_module,
        "time",
        SimpleNamespace(monotonic=lambda: start + Policy.SESSION_HOURS * 3600 + 1),
    )
    assert await store.get(hash_token(token)) is None


async def test_only_the_hash_of_the_token_is_stored(session):
    """A dump of this table must yield nothing that can be replayed."""
    await _user(session)

    token, row = await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)

    assert row.token_hash == hash_token(token)
    assert token not in row.token_hash


async def test_a_wrong_password_is_rejected(session):
    await _user(session)

    with pytest.raises(LoginFailed):
        await login(session, FakeSessionStore(), "a@x.com", "nope", now=NOW)


async def test_an_unknown_email_is_rejected_the_same_way(session):
    """Same exception, so the route cannot accidentally tell the two apart."""
    with pytest.raises(LoginFailed):
        await login(session, FakeSessionStore(), "nobody@x.com", "pw", now=NOW)


async def test_an_unknown_email_still_performs_a_hash(session, monkeypatch):
    """Otherwise the response time tells an attacker which addresses exist."""
    calls = {"n": 0}
    import auth.service as service_module

    real = service_module.verify_password

    def counting(plain, hashed):
        calls["n"] += 1
        return real(plain, hashed)

    monkeypatch.setattr(service_module, "verify_password", counting)

    with pytest.raises(LoginFailed):
        await login(session, FakeSessionStore(), "nobody@x.com", "pw", now=NOW)

    assert calls["n"] == 1


async def test_the_fifth_failure_locks_the_account(session):
    user = await _user(session)

    for _ in range(Policy.LOCK_AFTER_FAILURES):
        with pytest.raises(LoginFailed):
            await login(session, FakeSessionStore(), "a@x.com", "nope", now=NOW)

    assert user.locked_until == NOW + timedelta(seconds=Policy.LOCK_SECONDS)


async def test_a_locked_account_rejects_even_the_right_password(session):
    user = await _user(session)
    user.locked_until = NOW + timedelta(minutes=5)
    await session.flush()

    with pytest.raises(AccountLocked):
        await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)


async def test_the_lock_releases_once_its_window_passes(session):
    user = await _user(session)
    user.locked_until = NOW - timedelta(seconds=1)
    await session.flush()

    token, _ = await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)

    assert token


async def test_a_successful_login_clears_the_failure_count(session):
    user = await _user(session)
    user.failed_login_count = 3
    await session.flush()

    await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)

    assert user.failed_login_count == 0


async def test_logging_out_revokes_this_session_only(session):
    user = await _user(session)
    store = FakeSessionStore()
    first, first_row = await login(session, store, "a@x.com", "pw", now=NOW)
    second, _ = await login(session, store, "a@x.com", "pw", now=NOW)

    await logout(session, store, hash_token(first), now=NOW)

    assert await store.get(hash_token(first)) is None
    assert await store.get(hash_token(second)) is not None
    assert first_row.revoked_at == NOW
    assert user.id


async def test_logging_out_everywhere_revokes_every_session(session):
    user = await _user(session)
    store = FakeSessionStore()
    first, _ = await login(session, store, "a@x.com", "pw", now=NOW)
    second, _ = await login(session, store, "a@x.com", "pw", now=NOW)

    await logout_all(session, store, user.id, now=NOW)

    assert await store.get(hash_token(first)) is None
    assert await store.get(hash_token(second)) is None
