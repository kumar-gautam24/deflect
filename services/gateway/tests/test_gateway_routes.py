import httpx
import pytest_asyncio
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore
from doubles import build_upstream
from httpx import ASGITransport, AsyncClient

from gateway.main import app as gateway_app
from gateway.main import build_client, build_sessions

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


async def test_an_admin_session_reaches_an_operator_route(app):
    response = await call(
        app, "POST", "/ingest", headers={"Authorization": "Bearer admin-token"},
        json={"root": "/corpus"},
    )

    assert response.status_code != 401


async def test_health_and_ready_are_the_gateways_own(app):
    for path in ["/health", "/ready"]:
        response = await call(app, "GET", path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


async def test_the_gateways_own_metrics_needs_the_service_token(app):
    assert (await call(app, "GET", "/metrics")).status_code == 401
