import pytest
from fastapi import HTTPException

from deflect_common.auth import (
    Principal,
    bearer_guard,
    hash_token,
    resolve_principal,
    token_matches,
)
from deflect_common.sessions import FakeSessionStore


def test_a_correct_bearer_token_is_accepted():
    guard = bearer_guard("s3cret", "service")

    assert guard("Bearer s3cret") is None


@pytest.mark.parametrize(
    "header",
    [
        None,               # no header at all
        "",                 # empty header
        "Bearer wrong",     # right scheme, wrong token
        "Bearer ",          # right scheme, no token
        "s3cret",           # correct token, no scheme
        "Basic s3cret",     # correct token, wrong scheme
        "Bearer s3cret extra",  # trailing junk must not be trimmed into a match
    ],
)
def test_anything_other_than_the_exact_bearer_token_is_rejected(header):
    guard = bearer_guard("s3cret", "service")

    with pytest.raises(HTTPException) as raised:
        guard(header)

    assert raised.value.status_code == 401
    assert raised.value.headers["WWW-Authenticate"] == "Bearer"


def test_a_missing_credential_is_indistinguishable_from_a_wrong_one():
    """Telling a caller which of the two they got wrong is free information."""
    guard = bearer_guard("s3cret", "service")

    with pytest.raises(HTTPException) as missing:
        guard(None)
    with pytest.raises(HTTPException) as wrong:
        guard("Bearer wrong")

    assert missing.value.detail == wrong.value.detail
    assert missing.value.status_code == wrong.value.status_code


def test_the_principal_names_the_credential_that_was_expected():
    guard = bearer_guard("s3cret", "operator")

    with pytest.raises(HTTPException) as raised:
        guard(None)

    assert "operator" in raised.value.detail


def test_an_empty_expected_token_refuses_to_build_a_guard():
    """A service with an unset token must fail to start, not serve an open route."""
    with pytest.raises(ValueError, match="service"):
        bearer_guard("", "service")


def test_token_matches_is_exact():
    assert token_matches("s3cret", "Bearer s3cret") is True
    assert token_matches("s3cret", "Bearer s3cre") is False
    assert token_matches("s3cret", "bearer s3cret") is True  # scheme is case-insensitive
    assert token_matches("s3cret", None) is False


async def _store_with(token: str, user_id: str = "u1", role: str = "admin") -> FakeSessionStore:
    store = FakeSessionStore()
    await store.put(hash_token(token), user_id=user_id, role=role, ttl_seconds=300)
    return store


async def test_the_service_token_resolves_to_the_service_principal():
    found = await resolve_principal(
        "Bearer svc", service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found == Principal(kind="service")


async def test_the_operator_token_resolves_to_the_operator_principal():
    found = await resolve_principal(
        "Bearer op", service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found == Principal(kind="operator")


async def test_a_session_resolves_to_its_role_and_user():
    store = await _store_with("sess-abc", user_id="u7", role="viewer")

    found = await resolve_principal(
        "Bearer sess-abc", service_token="svc", operator_token="op", sessions=store
    )

    assert found == Principal(kind="session", role="viewer", user_id="u7")


async def test_an_unknown_token_resolves_to_nothing():
    found = await resolve_principal(
        "Bearer nope", service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found is None


async def test_a_missing_header_resolves_to_nothing():
    found = await resolve_principal(
        None, service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found is None


async def test_a_machine_token_never_costs_a_session_lookup():
    """The two comparisons short-circuit, so a service-to-service call does no I/O."""

    class ExplodingStore(FakeSessionStore):
        async def get(self, token_hash: str):
            raise AssertionError("a machine token must not reach the session store")

    assert await resolve_principal(
        "Bearer svc", service_token="svc", operator_token="op", sessions=ExplodingStore()
    ) == Principal(kind="service")


async def test_only_the_service_token_satisfies_service():
    """A logged-in human must never reach a machine-to-machine route."""
    from deflect_common.auth import satisfies

    assert satisfies(Principal(kind="service"), "service") is True
    assert satisfies(Principal(kind="operator"), "service") is False
    assert satisfies(Principal(kind="session", role="admin", user_id="u1"), "service") is False


async def test_operator_accepts_the_operator_token_or_an_admin_session():
    from deflect_common.auth import satisfies

    assert satisfies(Principal(kind="operator"), "operator") is True
    assert satisfies(Principal(kind="session", role="admin", user_id="u1"), "operator") is True
    assert satisfies(Principal(kind="session", role="viewer", user_id="u1"), "operator") is False
    # A service token is a machine, not an operator: collapsing them would let any
    # service trigger spend.
    assert satisfies(Principal(kind="service"), "operator") is False


async def test_viewer_accepts_any_valid_session_or_the_operator_token():
    from deflect_common.auth import satisfies

    assert satisfies(Principal(kind="session", role="viewer", user_id="u1"), "viewer") is True
    assert satisfies(Principal(kind="session", role="admin", user_id="u1"), "viewer") is True
    assert satisfies(Principal(kind="operator"), "viewer") is True
    assert satisfies(Principal(kind="service"), "viewer") is False


def test_hashing_a_token_is_stable_and_not_the_token():
    assert hash_token("abc") == hash_token("abc")
    assert "abc" not in hash_token("abc")
