from deflect_common.llm.fake import FakeClient
from httpx import ASGITransport, AsyncClient

from answer.main import app, build_client


async def get(path: str, headers: dict | None = None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def request(method: str, path: str, headers: dict | None = None, body: dict | None = None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json=body or {})


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
    failure amplifies instead of staying contained.

    The first assertion proves the address is genuinely unreachable -- without it this
    test would pass whether or not the property held, because a setup with no observable
    effect proves nothing. build_client is overridden because ASGITransport never runs
    the app's lifespan, so app.state.llm_client is unset; without this override /answer
    would 500 before ever reaching retrieval, and the first assertion would hold no
    matter what retrieval_url was, which is exactly the vacuous test this replaces.
    """
    from answer.config import get_settings

    original = get_settings().retrieval_url
    get_settings().__dict__["retrieval_url"] = "http://127.0.0.1:1"
    app.dependency_overrides[build_client] = lambda: FakeClient(["irrelevant"])
    try:
        # The address really is dead: a route that needs retrieval cannot succeed.
        answering = await request(
            "POST",
            "/answer",
            {"Authorization": "Bearer test-service-token"},
            {"question": "does this route need retrieval"},
        )
        assert answering.status_code >= 500

        # Readiness is nonetheless unaffected, because it checks only its own database.
        assert (await get("/ready")).status_code == 200
    finally:
        get_settings().__dict__["retrieval_url"] = original
        app.dependency_overrides.clear()
