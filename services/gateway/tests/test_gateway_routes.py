import httpx
import pytest_asyncio
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore
from doubles import build_upstream
from httpx import ASGITransport, AsyncClient

from gateway.breaker import CircuitBreaker
from gateway.main import app as gateway_app
from gateway.main import build_breaker, build_client, build_sessions
from gateway.policy import Policy

SERVICE = {"Authorization": "Bearer test-service-token"}
OPERATOR = {"Authorization": "Bearer test-operator-token"}


@pytest_asyncio.fixture
async def store():
    store = FakeSessionStore()
    await store.put(hash_token("admin-token"), user_id="7", role="admin", ttl_seconds=3600)
    return store


@pytest_asyncio.fixture
async def upstream():
    return build_upstream()


@pytest_asyncio.fixture
async def app(store, upstream):
    client = AsyncClient(transport=ASGITransport(app=upstream), base_url="http://upstream")
    gateway_app.dependency_overrides[build_sessions] = lambda: store
    gateway_app.dependency_overrides[build_client] = lambda: client
    # This fake upstream never returns a 502/504, so nothing here trips the breaker today --
    # but without an override these tests would share gateway.main's module-level singleton
    # with whatever else runs in the same session. Built once and captured, not inside the
    # lambda: FastAPI calls the override fresh per request, and a lambda that constructs the
    # breaker would hand each request its own amnesiac instance (test_breaker_wiring.py names
    # this exact pitfall).
    breaker = CircuitBreaker(Policy.BREAKER_FAILURES, Policy.BREAKER_COOLDOWN_SECONDS)
    gateway_app.dependency_overrides[build_breaker] = lambda: breaker
    yield gateway_app
    gateway_app.dependency_overrides.clear()
    await client.aclose()


async def call(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


async def test_metrics_never_reaches_an_upstream(app, upstream):
    """/metrics is absent from the route table, so it is unroutable rather than guarded.
    The gateway's own /metrics is a separate route; what must never happen is a proxied
    request to a service's /metrics.

    Asserted on the upstream's record of every path it was asked for, not on the response
    body -- a substring check there collides with the gateway's own metric names, and
    seen_headers alone would be vacuous because the double only sets it for paths it
    actually serves.
    """
    await call(app, "GET", "/metrics", headers=SERVICE)

    assert upstream.state.seen_paths == []


async def test_an_unknown_path_is_a_404_before_any_upstream_call(app, upstream):
    response = await call(app, "GET", "/nope", headers=OPERATOR)

    assert response.status_code == 404
    assert upstream.state.seen_paths == []


async def test_an_upstream_docs_path_is_not_routed(app, upstream):
    """Same reasoning. /redoc and /openapi.json belong to the gateway's own FastAPI app
    when ENV is not production, and must never become a proxied request to a service.

    Not asserted on the body: FastAPI puts every handler's docstring into the OpenAPI
    schema, so any word this codebase uses in a docstring -- "upstream" among them -- will
    appear in /openapi.json for reasons that have nothing to do with routing.
    """
    for path in ["/redoc", "/openapi.json"]:
        await call(app, "GET", path, headers=OPERATOR)

    assert upstream.state.seen_paths == []


async def test_a_guarded_route_refuses_a_missing_credential(app):
    response = await call(app, "POST", "/ingest", json={"root": "/corpus"})

    assert response.status_code == 401


async def test_a_guarded_route_refuses_the_wrong_credential(app):
    response = await call(app, "POST", "/ingest", headers=SERVICE, json={"root": "/corpus"})

    assert response.status_code == 401


async def test_an_admin_session_reaches_an_operator_route(app, upstream):
    """The double has no /ingest route, so this is 404 either way -- proxied or refused at
    401 before reaching the upstream. Asserting != 401 alone would pass for almost any
    breakage that also happens to 404. What only a genuinely proxied request produces is
    the upstream's own record of being asked for it, so assert on that instead.
    """
    response = await call(
        app, "POST", "/ingest", headers={"Authorization": "Bearer admin-token"},
        json={"root": "/corpus"},
    )

    assert response.status_code == 404
    assert upstream.state.seen_paths == ["/ingest"]


async def test_a_minted_request_id_is_forwarded_to_the_upstream(app, upstream):
    """RequestIdMiddleware mints an id when the caller sends none, and puts it on the
    contextvar and the response -- but clean_request_headers only ever copies INBOUND
    headers, so with no id supplied nothing carried the minted one to the upstream before
    this fix. Proven live: one request logged a different id at the gateway and at evals.
    """
    response = await call(app, "GET", "/eval-runs")

    assert upstream.state.seen_headers["x-request-id"] == response.headers["X-Request-ID"]


async def test_health_and_ready_are_the_gateways_own(app):
    for path in ["/health", "/ready"]:
        response = await call(app, "GET", path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


async def test_the_gateways_own_metrics_needs_the_service_token(app):
    assert (await call(app, "GET", "/metrics")).status_code == 401
