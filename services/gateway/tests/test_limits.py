import math

import httpx
import pytest_asyncio
from deflect_common.ratelimit import InMemoryLeakyBucket
from deflect_common.sessions import FakeSessionStore
from doubles import build_upstream
from httpx import ASGITransport, AsyncClient

import gateway.main as gateway_main
from gateway.config import get_settings
from gateway.main import app as gateway_app
from gateway.main import build_client, build_limiters, build_sessions
from gateway.policy import Policy

ASK_RATE = get_settings().ask_rate_limit_per_hour
SERVICE = {"Authorization": f"Bearer {get_settings().service_token}"}


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

    # > 0 is guaranteed by the handler's own max(1, ...) and proves nothing about this
    # response in particular; the status code is what proves a refusal actually happened.
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


async def test_the_advertised_wait_is_honest():
    """The old version only asserted Retry-After < WINDOW_SECONDS, which passes whether or
    not the value is honest -- and it was not: live, a 39s advertised wait was still
    refused after sleeping exactly 39s, because int() truncates a fractional retry_after
    down.

    The refusal is deliberately checked at a FRACTIONAL now (0.5s after a fill done all at
    once): filling and refusing at the same instant makes retry_after an exact multiple of
    the drain interval, where int() and math.ceil() happen to agree and this test would
    pass either way -- every real request lands at an irregular wall-clock instant, which
    is exactly why the live regression could never have shown up against that clean case.

    This drives the limiter's own clock forward by exactly what the handler would
    advertise -- max(1, math.ceil(retry_after)), the same arithmetic main.py uses for the
    header -- and proves the next call is allowed. No sleeping: `now` is already a
    parameter on the limiter.
    """
    limiter = InMemoryLeakyBucket(ASK_RATE, Policy.WINDOW_SECONDS, Policy.ASK_BURST)
    for _ in range(Policy.ASK_BURST):
        await limiter.check("addr", now=0.0)

    refused = await limiter.check("addr", now=0.5)
    assert refused.allowed is False
    assert refused.retry_after % 1 != 0, "this scenario must be fractional to prove anything"

    advertised = max(1, math.ceil(refused.retry_after))
    assert (await limiter.check("addr", now=0.5 + advertised)).allowed is True


async def test_the_gateways_own_header_is_the_wait_that_actually_works(app, monkeypatch):
    """The test above proves ceil() is the right rounding in isolation; this proves
    main.py's handler is actually the code applying it, by driving the REAL handler's
    clock -- not a bare limiter's -- forward by exactly what its own Retry-After header
    said, and confirming the request that follows is let through.
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(gateway_main.time, "monotonic", lambda: clock["now"])

    for _ in range(Policy.ASK_BURST):
        assert (await call(app, "/ask")).status_code != 429

    # Fractional, for the same reason as above: filling and refusing at the same instant
    # produces an exact multiple of the drain interval, where a truncating int() and the
    # correct math.ceil() would advertise the same number and this test would prove nothing.
    clock["now"] = 0.5
    refused = await call(app, "/ask")
    assert refused.status_code == 429

    advertised = int(refused.headers["Retry-After"])
    clock["now"] = 0.5 + advertised

    assert (await call(app, "/ask")).status_code != 429


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


async def test_a_service_token_caller_is_trusted_leftmost_behind_the_edge_proxy(app, monkeypatch):
    """The exact production shape: the BFF authenticates with SERVICE_TOKEN and overwrites
    X-Forwarded-For with the visitor's single real address; Render's own load balancer then
    appends its own hop, making a two-entry header. Without the service-token check the
    gateway would trust edge_address's rightmost entry -- the constant BFF egress hop --
    and every visitor behind it would share one bucket, which is exactly C1.
    """
    monkeypatch.setattr(gateway_main._settings, "trusted_proxy_hops", 1)

    for i in range(Policy.ASK_BURST):
        response = await call(
            app,
            "/ask",
            headers={"X-Forwarded-For": f"9.9.9.{i}, 203.0.113.9", **SERVICE},
        )
        assert response.status_code != 429

    # A distinct visitor behind the same egress hop gets its own allowance rather than
    # sharing whatever the trailing entry's bucket had already drained.
    response = await call(
        app, "/ask", headers={"X-Forwarded-For": "9.9.9.250, 203.0.113.9", **SERVICE}
    )
    assert response.status_code != 429


async def test_a_service_token_caller_sharing_a_leftmost_entry_shares_one_bucket(
    app, monkeypatch
):
    monkeypatch.setattr(gateway_main._settings, "trusted_proxy_hops", 1)

    for _ in range(Policy.ASK_BURST):
        response = await call(
            app, "/ask", headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.9", **SERVICE}
        )
        assert response.status_code != 429

    response = await call(
        app, "/ask", headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.9", **SERVICE}
    )
    assert response.status_code == 429


async def test_an_unguarded_route_is_not_limited(app):
    """Only routes naming a limiter are limited. /eval-runs is public and cheap."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(ASK_RATE + 5):
            assert (await c.get("/eval-runs")).status_code != 429
