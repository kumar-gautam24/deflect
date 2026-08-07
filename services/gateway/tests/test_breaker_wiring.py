"""The unit tests in test_breaker.py prove CircuitBreaker's own rules against a clock
parameter. They prove nothing about main.py's wiring: an is_open check that is never
called, or one wired to the wrong upstream key, would pass every one of them.

This file is the load-bearing check for the wiring itself: a route whose upstream keeps
failing must eventually get a 503 from the gateway without the upstream being dialled
again. The gateway is exercised as a real ASGI app, the same way test_gateway_routes.py
does, so the assertion is about main.py's handler and not about CircuitBreaker directly.
"""

import httpx
import pytest_asyncio
from deflect_common.sessions import FakeSessionStore
from httpx import ASGITransport, AsyncClient

from gateway.breaker import CircuitBreaker
from gateway.main import app as gateway_app
from gateway.main import build_breaker, build_client, build_sessions
from gateway.policy import Policy

# /eval-runs is public and unrated: no session and no limiter allowance is spent reaching
# it, so every 503-vs-502 distinction below is attributable to the breaker alone.
PATH = "/eval-runs"


class _CountingTransport(httpx.AsyncBaseTransport):
    """Stands in for a socket to a dead upstream, counting every attempt to use it.

    A real unreachable address (port 1, say) also produces a 502, but nothing about that
    setup can prove how many times the gateway actually tried to connect -- which is
    exactly the fact this test exists to establish. Counting handle_async_request calls
    is the only direct way to see whether the gateway dialled out.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError("connection refused", request=request)


@pytest_asyncio.fixture
async def app():
    transport = _CountingTransport()
    client = AsyncClient(transport=transport, base_url="http://dead-upstream")
    gateway_app.dependency_overrides[build_sessions] = lambda: FakeSessionStore()
    gateway_app.dependency_overrides[build_client] = lambda: client
    # A fresh breaker per test, the same reasoning as _fresh_limiters in test_limits.py:
    # the module-level one is shared for the process, so a test that opens it would leave
    # it open for whatever runs next. Built once and captured, not built inside the
    # lambda -- FastAPI calls the override fresh on every request, so a lambda that
    # constructs the breaker would hand each request its own amnesiac instance and the
    # failure count would never accumulate.
    breaker = CircuitBreaker(Policy.BREAKER_FAILURES, Policy.BREAKER_COOLDOWN_SECONDS)
    gateway_app.dependency_overrides[build_breaker] = lambda: breaker
    yield gateway_app, transport
    gateway_app.dependency_overrides.clear()
    await client.aclose()


async def call(app, path: str) -> httpx.Response:
    asgi_transport = ASGITransport(app=app)
    async with AsyncClient(transport=asgi_transport, base_url="http://test") as c:
        return await c.get(path)


async def test_a_route_whose_upstream_keeps_failing_stops_being_dialled(app):
    gateway, upstream_transport = app

    # The threshold number of consecutive failures: each one really reaches the dead
    # upstream and comes back as a 502, which is what trips the breaker open.
    for _ in range(Policy.BREAKER_FAILURES):
        response = await call(gateway, PATH)
        assert response.status_code == 502
    assert upstream_transport.calls == Policy.BREAKER_FAILURES

    # The breaker is now open. The gateway must refuse locally rather than dial out again.
    response = await call(gateway, PATH)

    assert response.status_code == 503
    assert upstream_transport.calls == Policy.BREAKER_FAILURES
