import httpx
import pytest_asyncio
from deflect_common.sessions import FakeSessionStore
from doubles import build_upstream
from httpx import ASGITransport, AsyncClient

import gateway.main as gateway_main
from gateway.config import get_settings
from gateway.main import app as gateway_app
from gateway.main import build_client, build_limiters, build_sessions
from gateway.policy import Policy

ASK_RATE = get_settings().ask_rate_limit_per_hour


@pytest_asyncio.fixture
async def app():
    upstream = build_upstream()
    client = AsyncClient(transport=ASGITransport(app=upstream), base_url="http://upstream")
    gateway_app.dependency_overrides[build_sessions] = lambda: FakeSessionStore()
    gateway_app.dependency_overrides[build_client] = lambda: client
    # Captured once, not called inside the lambda: FastAPI invokes an override callable
    # fresh on every request rather than caching it across requests, so `lambda:
    # _fresh_limiters()` would hand each request its own empty limiter and the allowance
    # would never appear spent.
    limiters = _fresh_limiters()
    gateway_app.dependency_overrides[build_limiters] = lambda: limiters
    yield gateway_app
    gateway_app.dependency_overrides.clear()
    await client.aclose()


def _fresh_limiters():
    """A new set per test: a module-level singleton shared across a session makes one
    test's traffic another test's failure."""
    from deflect_common.ratelimit import InMemoryLeakyBucket

    return {
        "ask": InMemoryLeakyBucket(ASK_RATE, Policy.WINDOW_SECONDS, Policy.ASK_BURST),
        "login": InMemoryLeakyBucket(
            Policy.LOGIN_PER_HOUR, Policy.WINDOW_SECONDS, Policy.LOGIN_BURST
        ),
    }


async def call(app, path: str, headers=None) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(path, json={"question": "q"}, headers=headers or {})


async def test_the_allowance_is_spent_and_then_refused(app):
    for _ in range(Policy.ASK_BURST):
        assert (await call(app, "/ask")).status_code != 429

    assert (await call(app, "/ask")).status_code == 429


async def test_a_refusal_carries_retry_after(app):
    for _ in range(ASK_RATE):
        await call(app, "/ask")

    response = await call(app, "/ask")

    assert int(response.headers["Retry-After"]) > 0


async def test_the_advertised_wait_is_honest(app):
    for _ in range(Policy.ASK_BURST):
        await call(app, "/ask")

    response = await call(app, "/ask")

    assert response.status_code == 429
    # An hour would be the old hardcoded answer; the real one is one emission interval.
    assert int(response.headers["Retry-After"]) < Policy.WINDOW_SECONDS


async def test_a_spoofed_forwarded_header_does_not_buy_a_fresh_allowance(app):
    """The regression test for edge_address. If the gateway believed the leftmost entry,
    each of these would be a new key and the limit would never bind."""
    for i in range(Policy.ASK_BURST):
        response = await call(app, "/ask", headers={"X-Forwarded-For": f"9.9.9.{i}"})
        assert response.status_code != 429

    response = await call(app, "/ask", headers={"X-Forwarded-For": "9.9.9.250"})

    assert response.status_code == 429


async def test_a_spoofed_leading_entry_does_not_evade_the_trailing_trusted_one(app, monkeypatch):
    """Every other test in this file runs under TRUSTED_PROXY_HOPS=0 (see conftest.py),
    which is right for a bare in-process call but never exercises the production shape:
    one real proxy in front, appending the address it saw the connection from. This test
    overrides the hop count to 1 and sends a caller-supplied leading entry plus a fixed
    trailing one, standing in for that append. The leading entry varies per request; if
    the gateway kept believing it instead of the trailing one, each request would buy its
    own fresh allowance and the limit would never bind.
    """
    monkeypatch.setattr(gateway_main._settings, "trusted_proxy_hops", 1)

    for i in range(Policy.ASK_BURST):
        response = await call(
            app, "/ask", headers={"X-Forwarded-For": f"9.9.9.{i}, 203.0.113.9"}
        )
        assert response.status_code != 429

    response = await call(app, "/ask", headers={"X-Forwarded-For": "9.9.9.250, 203.0.113.9"})

    assert response.status_code == 429


async def test_an_unguarded_route_is_not_limited(app):
    """Only routes naming a limiter are limited. /eval-runs is public and cheap."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(ASK_RATE + 5):
            assert (await c.get("/eval-runs")).status_code != 429
