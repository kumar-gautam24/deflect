import pytest

from deflect_common.sessions import FakeSessionStore, RedisSessionStore


async def test_a_stored_session_resolves_to_its_user_and_role():
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=300)

    assert await store.get("hash-a") == ("u1", "admin")


async def test_an_unknown_hash_resolves_to_nothing():
    assert await FakeSessionStore().get("nope") is None


async def test_deleting_a_session_revokes_it():
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=300)

    await store.delete("hash-a")

    assert await store.get("hash-a") is None


async def test_deleting_a_user_revokes_every_session_they_hold():
    """Logging out everywhere has to reach sessions this process never issued."""
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=300)
    await store.put("hash-b", user_id="u1", role="admin", ttl_seconds=300)
    await store.put("hash-c", user_id="u2", role="viewer", ttl_seconds=300)

    await store.delete_user("u1")

    assert await store.get("hash-a") is None
    assert await store.get("hash-b") is None
    assert await store.get("hash-c") == ("u2", "viewer")


async def test_an_expired_session_resolves_to_nothing():
    """The TTL carries the session's own lifetime, so expiry has to be real rather than
    advisory -- this store is what ends a session everywhere except the auth service."""
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=0)

    assert await store.get("hash-a") is None


class _RedisReturning:
    """Just enough of the client for get(), handing back a value we chose."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    async def get(self, key: str) -> str:
        return self._raw


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"user_id": "u1"}',  # role missing
        '["u1", "admin"]',  # right values, wrong shape
        "null",
    ],
    ids=["malformed", "missing-role", "wrong-shape", "null"],
)
async def test_a_value_that_cannot_be_read_resolves_to_nothing(raw):
    """One unreadable value fails one request rather than 500ing every guarded route.

    The store is read by four services, so an exception escaping here would take all of
    them down at once. from_url is lazy, so building the store contacts nothing.
    """
    store = RedisSessionStore("redis://localhost:6379/0")
    store._redis = _RedisReturning(raw)

    assert await store.get("hash-a") is None
