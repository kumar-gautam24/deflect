from httpx import ASGITransport, AsyncClient

from evals.main import app


async def get(path: str, headers: dict | None = None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def test_liveness_touches_no_dependency():
    """A liveness probe that queries the database restarts a healthy process whenever
    Postgres hiccups, which is the opposite of what it is for."""
    response = await get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_the_database():
    response = await get("/ready")

    assert response.status_code == 200
    assert response.json()["database"] == "connected"


async def test_liveness_is_public():
    assert (await get("/health")).status_code != 401


async def test_readiness_is_public():
    assert (await get("/ready")).status_code != 401
