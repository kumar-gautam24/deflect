from deflect_common.sessions import FakeSessionStore


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
    """The TTL is the ceiling on how long a revoked session survives a missed delete, so
    expiry has to be real rather than advisory."""
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=0)

    assert await store.get("hash-a") is None
