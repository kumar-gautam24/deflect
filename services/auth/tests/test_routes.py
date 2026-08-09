from datetime import UTC, datetime, timedelta

import httpx
import pytest_asyncio
from deflect_common.ratelimit import SlidingWindowLimiter
from deflect_common.sessions import FakeSessionStore
from httpx import ASGITransport, AsyncClient

from auth.db import get_session
from auth.main import app as fastapi_app
from auth.main import build_login_backstop, build_sessions
from auth.models import AdminUser
from auth.passwords import hash_password
from auth.policy import Policy


async def _user(session, email="a@x.com", password="pw", role="admin") -> AdminUser:
    user = AdminUser(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def app(session):
    session.commit = session.flush
    fastapi_app.dependency_overrides[get_session] = lambda: session
    fastapi_app.dependency_overrides[build_sessions] = lambda: FakeSessionStore()
    # A fresh backstop per test, not the module-level singleton it replaces here: sharing
    # one instance would let one test's login attempts spend another test's allowance.
    # Captured once, not built inside the lambda: FastAPI calls an override callable fresh
    # on every request, so a lambda that constructs the limiter would hand each request
    # its own empty one and it could never accumulate even within a single test.
    backstop = SlidingWindowLimiter(Policy.LOGIN_BACKSTOP_PER_HOUR, window_seconds=3600)
    fastapi_app.dependency_overrides[build_login_backstop] = lambda: backstop
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


async def call(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


async def test_a_correct_login_returns_a_token(session, app):
    await _user(session)

    response = await call(
        app, "POST", "/auth/login", json={"email": "a@x.com", "password": "pw"}
    )

    assert response.status_code == 200
    assert response.json()["token"]


async def test_the_backstop_rejects_once_its_own_budget_is_spent(session, app):
    """This is the direct-door control, not the gateway's precise per-visitor login
    bucket: a caller reaching /auth/login on :8004 directly, bypassing the gateway's
    limiter entirely, still gets throttled rather than running an unconditional argon2id
    hash forever.
    """
    await _user(session)
    # Captured once, not built inside the lambda: FastAPI invokes an override callable
    # fresh on every request rather than caching it, so a lambda that constructs the
    # limiter would hand each request its own empty one and it could never actually trip.
    tiny_backstop = SlidingWindowLimiter(1, window_seconds=3600)
    fastapi_app.dependency_overrides[build_login_backstop] = lambda: tiny_backstop

    first = await call(app, "POST", "/auth/login", json={"email": "a@x.com", "password": "pw"})
    second = await call(app, "POST", "/auth/login", json={"email": "a@x.com", "password": "pw"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0


async def test_a_wrong_password_and_an_unknown_email_return_the_same_body(session, app):
    """The route must not let a client tell the two failure cases apart."""
    await _user(session)

    wrong = await call(
        app, "POST", "/auth/login", json={"email": "a@x.com", "password": "nope"}
    )
    unknown = await call(
        app, "POST", "/auth/login", json={"email": "nobody@x.com", "password": "pw"}
    )

    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_a_locked_account_is_indistinguishable_from_an_unknown_one(session, app):
    """A lockout must not answer "does this address have an admin account?".

    AccountLocked is raised only for a row that exists, so any reply that differs from the
    unknown-address one identifies an admin in five requests -- the exact leak the shared
    401 above is built to prevent. A Retry-After header would give it away just as
    completely as a distinct status code.
    """
    user = await _user(session, email="locked@x.com")
    user.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.flush()

    locked = await call(
        app, "POST", "/auth/login", json={"email": "locked@x.com", "password": "pw"}
    )
    unknown = await call(
        app, "POST", "/auth/login", json={"email": "nobody@x.com", "password": "pw"}
    )

    assert locked.status_code == 401
    assert unknown.status_code == 401
    assert locked.json() == unknown.json()
    assert "Retry-After" not in locked.headers


async def test_a_malformed_login_body_never_echoes_the_password(session, app):
    """Misspelling a field must not send the password back out.

    Pydantic reports the offending input with each error, and for a missing field that
    input is the entire body -- so the default 422 hands the plaintext password to the
    caller and to every proxy log and error tracker the response crosses. A password is
    supposed to exist nowhere but its hash.
    """
    response = await call(
        app, "POST", "/auth/login", json={"emial": "a@x.com", "password": "hunter2"}
    )

    assert response.status_code == 422
    assert "hunter2" not in response.text
    # Still says which field was wrong -- stripping the value must not cost the diagnosis.
    assert "email" in response.text


async def test_me_returns_the_role_for_a_valid_session(session, app):
    await _user(session)
    login_response = await call(
        app, "POST", "/auth/login", json={"email": "a@x.com", "password": "pw"}
    )
    token = login_response.json()["token"]

    response = await call(app, "GET", "/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_me_requires_a_session(app):
    response = await call(app, "GET", "/auth/me")

    assert response.status_code == 401


async def test_logout_makes_the_session_stop_working(session, app):
    await _user(session)
    login_response = await call(
        app, "POST", "/auth/login", json={"email": "a@x.com", "password": "pw"}
    )
    token = login_response.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    logout_response = await call(app, "POST", "/auth/logout", headers=headers)
    me_response = await call(app, "GET", "/auth/me", headers=headers)

    assert logout_response.status_code == 200
    assert me_response.status_code == 401


async def test_metrics_requires_the_service_token(app):
    response = await call(app, "GET", "/metrics")

    assert response.status_code == 401


async def test_metrics_accepts_the_service_token(app):
    response = await call(
        app, "GET", "/metrics", headers={"Authorization": "Bearer test-service-token"}
    )

    assert response.status_code == 200


async def test_health_is_public(app):
    response = await call(app, "GET", "/health")

    assert response.status_code == 200


async def test_ready_is_public(app):
    response = await call(app, "GET", "/ready")

    assert response.status_code == 200
