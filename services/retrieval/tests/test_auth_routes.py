"""One case per protected route.

Guards are easy to remove by accident during a refactor. A test per route means a
deleted guard fails the build rather than silently opening an endpoint.
"""

from httpx import ASGITransport, AsyncClient

from retrieval.main import app

SERVICE = {"Authorization": "Bearer test-service-token"}
OPERATOR = {"Authorization": "Bearer test-operator-token"}


async def request(method: str, path: str, headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json={})


async def test_documents_requires_a_credential():
    assert (await request("GET", "/documents")).status_code == 401


async def test_search_requires_a_credential():
    assert (await request("POST", "/search")).status_code == 401


async def test_ingest_requires_a_credential():
    assert (await request("POST", "/ingest")).status_code == 401


async def test_health_stays_public_because_render_polls_it_unauthenticated():
    assert (await request("GET", "/health")).status_code == 200


async def test_the_operator_token_does_not_open_a_service_route():
    """The two principals are distinct; one credential is not a master key."""
    assert (await request("GET", "/documents", OPERATOR)).status_code == 401


async def test_the_service_token_does_not_open_ingest():
    assert (await request("POST", "/ingest", SERVICE)).status_code == 401


async def test_a_service_credential_reaches_the_documents_handler():
    assert (await request("GET", "/documents", SERVICE)).status_code == 200
