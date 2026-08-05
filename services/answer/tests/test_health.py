from httpx import ASGITransport, AsyncClient

from answer.main import app


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


async def test_readiness_ignores_an_unreachable_retrieval_service():
    """A readiness check that follows its dependencies turns one service's outage into
    all three reporting unready, so an orchestrator restarts healthy processes and the
    failure amplifies. Retrieval being down is already a 503 on the request that needed
    it."""
    from answer.config import get_settings

    get_settings.cache_clear()
    original = get_settings().retrieval_url
    try:
        get_settings().__dict__["retrieval_url"] = "http://127.0.0.1:1"
        assert (await get("/ready")).status_code == 200
    finally:
        get_settings().__dict__["retrieval_url"] = original
        get_settings.cache_clear()
