import pytest
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore

from gateway.principal import allowed
from gateway.routes import ROUTES, Route

SERVICE = "test-service-token"
OPERATOR = "test-operator-token"


async def _store_with(role: str | None) -> tuple[FakeSessionStore, str | None]:
    store = FakeSessionStore()
    if role is None:
        return store, None
    token = f"session-{role}"
    await store.put(hash_token(token), user_id="7", role=role, ttl_seconds=3600)
    return store, token


async def _decide(route: Route, credential: str | None, role: str | None = None) -> bool:
    store, session_token = await _store_with(role)
    value = session_token if credential == "session" else credential
    header = f"Bearer {value}" if value else None
    return await allowed(route, header, store, SERVICE, OPERATOR)


PUBLIC = Route("POST", "/ask", "answer", None)
SERVICE_ONLY = Route("POST", "/search", "retrieval", "service")
OPERATOR_ONLY = Route("POST", "/ingest", "retrieval", "operator")
VIEWER_OK = Route("GET", "/traces", "answer", "viewer")
SESSION_ONLY = Route("GET", "/auth/me", "auth", "session")


async def test_a_public_route_needs_nothing():
    assert await _decide(PUBLIC, None) is True


async def test_a_public_route_still_admits_a_credentialled_caller():
    assert await _decide(PUBLIC, SERVICE) is True


@pytest.mark.parametrize(
    ("route", "credential", "role", "expected"),
    [
        (SERVICE_ONLY, SERVICE, None, True),
        (SERVICE_ONLY, OPERATOR, None, False),
        (SERVICE_ONLY, "session", "admin", False),
        (OPERATOR_ONLY, OPERATOR, None, True),
        (OPERATOR_ONLY, SERVICE, None, False),
        (OPERATOR_ONLY, "session", "admin", True),
        (OPERATOR_ONLY, "session", "viewer", False),
        (VIEWER_OK, OPERATOR, None, True),
        (VIEWER_OK, "session", "viewer", True),
        (VIEWER_OK, "session", "admin", True),
        (VIEWER_OK, SERVICE, None, False),
    ],
    ids=lambda v: str(v),
)
async def test_the_matrix_matches_the_shared_rules(route, credential, role, expected):
    """The gateway must reach the same verdict as the service behind it. A gateway that
    was stricter would break a caller the service accepts; one that was looser would send
    traffic that can only 401, and hide which layer refused it."""
    assert await _decide(route, credential, role) is expected


async def test_a_service_token_never_satisfies_operator():
    """Collapsing machines and operators would let any service trigger spend."""
    assert await _decide(OPERATOR_ONLY, SERVICE) is False


@pytest.mark.parametrize("credential", [SERVICE, OPERATOR])
async def test_a_token_does_not_satisfy_a_session_route(credential):
    """/auth/logout revokes a session row, and a token has none to revoke. Admitting a
    token here would reach a route that can only fail."""
    assert await _decide(SESSION_ONLY, credential) is False


async def test_any_valid_session_satisfies_a_session_route():
    assert await _decide(SESSION_ONLY, "session", "viewer") is True
    assert await _decide(SESSION_ONLY, "session", "admin") is True


async def test_an_unknown_session_is_refused():
    store = FakeSessionStore()

    assert await allowed(VIEWER_OK, "Bearer nonsense", store, SERVICE, OPERATOR) is False


async def test_a_malformed_header_is_refused():
    store = FakeSessionStore()

    for header in ["", "Bearer", "Basic abc", "sess-abc", "Bearer "]:
        assert await allowed(VIEWER_OK, header, store, SERVICE, OPERATOR) is False


async def test_an_unknown_role_fails_closed():
    """A role written straight into the database must refuse rather than raise."""
    store = FakeSessionStore()
    await store.put(hash_token("odd"), user_id="7", role="superuser", ttl_seconds=3600)

    assert await allowed(VIEWER_OK, "Bearer odd", store, SERVICE, OPERATOR) is False


async def test_every_route_in_the_table_is_decidable():
    """No route may name a principal the resolver cannot evaluate."""
    store = FakeSessionStore()

    for route in ROUTES:
        assert await allowed(route, None, store, SERVICE, OPERATOR) in (True, False)
