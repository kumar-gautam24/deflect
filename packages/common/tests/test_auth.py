import pytest
from fastapi import HTTPException

from deflect_common.auth import bearer_guard, token_matches


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
