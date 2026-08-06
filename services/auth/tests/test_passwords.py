import pytest

from auth.passwords import hash_password, needs_rehash, verify_password


def test_a_password_verifies_against_its_own_hash():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed) is True


def test_a_wrong_password_does_not_verify():
    assert verify_password("wrong", hash_password("right")) is False


def test_the_hash_does_not_contain_the_password():
    """The obvious property, asserted because getting it wrong is catastrophic and
    silent."""
    assert "hunter2" not in hash_password("hunter2")


def test_two_hashes_of_one_password_differ():
    """Per-hash salt. Identical hashes would let anyone see which accounts share a
    password."""
    assert hash_password("same") != hash_password("same")


def test_the_hash_names_argon2id():
    assert hash_password("x").startswith("$argon2id$")


def test_verifying_against_a_malformed_hash_is_false_rather_than_an_error():
    """A corrupted row must fail the login, not crash the endpoint."""
    assert verify_password("x", "not-a-hash") is False


@pytest.mark.parametrize("empty", ["", None])
def test_verifying_without_a_stored_hash_is_false(empty):
    """An account with no password set must never authenticate."""
    assert verify_password("x", empty) is False
