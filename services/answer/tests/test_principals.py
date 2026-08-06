from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore
from httpx import ASGITransport, AsyncClient

from answer.main import app, build_sessions

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
    assert (await request("GET", "/traces", OPERATOR)).status_code != 401


async def test_an_admin_session_can_read_traces():
    headers, store = _with_session("admin")
    await store.put(hash_token("sess-token"), user_id="1", role="admin", ttl_seconds=300)
    try:
        assert (await request("GET", "/traces", headers)).status_code != 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)


async def test_a_viewer_session_can_read_traces():
    """/traces moved from operator to viewer, so a session with the lesser role must be
    accepted -- that widening is the whole point of this route's change."""
    headers, store = _with_session("viewer")
    await store.put(hash_token("sess-token"), user_id="1", role="viewer", ttl_seconds=300)
    try:
        assert (await request("GET", "/traces", headers)).status_code != 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)


async def test_an_anonymous_caller_still_cannot_read_traces():
    assert (await request("GET", "/traces")).status_code == 401


async def test_a_viewer_session_cannot_reach_the_service_only_answer_route():
    headers, store = _with_session("viewer")
    await store.put(hash_token("sess-token"), user_id="1", role="viewer", ttl_seconds=300)
    try:
        assert (await request("POST", "/answer", headers)).status_code == 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)
