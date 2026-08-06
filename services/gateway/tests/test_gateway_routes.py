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
async def app(store):
    upstream = build_upstream()
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


async def test_metrics_is_not_routable_to_an_upstream(app):
    """It is absent from the table, so it cannot be reached even by a caller holding the
    right token. The gateway's own /metrics is a different route, added below."""
    response = await call(app, "GET", "/metrics", headers=SERVICE)

    assert response.status_code in (200, 401)
    # 200 only from the gateway's OWN metrics; never a proxied upstream body.
    assert "path" not in response.text


async def test_an_unknown_path_is_a_404_before_any_upstream_call(app):
    response = await call(app, "GET", "/nope", headers=OPERATOR)

    assert response.status_code == 404


async def test_an_upstream_docs_path_is_not_routed(app):
    for path in ["/redoc", "/openapi.json"]:
        response = await call(app, "GET", path, headers=OPERATOR)
        assert response.status_code in (200, 404)
        assert "upstream" not in response.text


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
