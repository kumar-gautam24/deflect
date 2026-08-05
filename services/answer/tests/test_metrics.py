from httpx import ASGITransport, AsyncClient

from answer.main import app


async def get(path: str, headers: dict | None = None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def test_metrics_requires_a_service_credential():
    """Request volumes and latencies are operational intelligence. The policy table is
    'the security policy in full', so a new public endpoint would be exactly the drift
    it exists to prevent."""
    assert (await get("/metrics")).status_code == 401


async def test_metrics_serves_a_prometheus_exposition_to_a_service_caller():
    response = await get("/metrics", {"Authorization": "Bearer test-service-token"})

    assert response.status_code == 200
    assert "python_info" in response.text


async def test_an_operator_credential_does_not_open_metrics():
    response = await get("/metrics", {"Authorization": "Bearer test-operator-token"})

    assert response.status_code == 401
