from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore
from httpx import ASGITransport, AsyncClient

from retrieval.main import app, build_sessions

OPERATOR = {"Authorization": "Bearer test-operator-token"}
SERVICE = {"Authorization": "Bearer test-service-token"}


async def request(method: str, path: str, headers=None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json={})


def _with_session(role: str) -> tuple[dict, FakeSessionStore]:
    store = FakeSessionStore()
    app.dependency_overrides[build_sessions] = lambda: store
    return {"Authorization": "Bearer sess-token"}, store


async def test_the_operator_token_still_opens_every_route_it_did():
    """CI holds this token and nothing about CI may change."""
    assert (await request("GET", "/jobs/1", OPERATOR)).status_code != 401


async def test_an_admin_session_can_reach_an_operator_route():
    headers, store = _with_session("admin")
    await store.put(hash_token("sess-token"), user_id="1", role="admin", ttl_seconds=300)
    try:
        assert (await request("GET", "/jobs/1", headers)).status_code != 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)


async def test_a_viewer_session_cannot_reach_an_operator_route():
    headers, store = _with_session("viewer")
    await store.put(hash_token("sess-token"), user_id="1", role="viewer", ttl_seconds=300)
    try:
        assert (await request("GET", "/jobs/1", headers)).status_code == 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)


async def test_an_anonymous_caller_still_cannot_reach_an_operator_route():
    assert (await request("GET", "/jobs/1")).status_code == 401


async def test_a_session_is_rejected_by_the_service_only_search_route():
    """POST /search is service-only; even an admin session must not open it."""
    headers, store = _with_session("admin")
    await store.put(hash_token("sess-token"), user_id="1", role="admin", ttl_seconds=300)
    try:
        assert (await request("POST", "/search", headers)).status_code == 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)
