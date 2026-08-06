"""Session storage shared by every service that checks one.

Postgres is the record and this is the working copy. Services read here rather than
querying the auth database, so no service depends on auth being reachable to serve its own
data -- you simply cannot log in while it is down.

The cost is that revocation is bounded rather than instant: if a delete is missed, a
session stays usable until its entry expires. That is why the TTL is short.
"""

import json
import time
from typing import Protocol

import redis.asyncio as aioredis

_PREFIX = "session:"
_USER_PREFIX = "session-user:"


class SessionStore(Protocol):
    async def get(self, token_hash: str) -> tuple[str, str] | None: ...

    async def put(self, token_hash: str, user_id: str, role: str, ttl_seconds: int) -> None: ...

    async def delete(self, token_hash: str) -> None: ...

    async def delete_user(self, user_id: str) -> None: ...


class RedisSessionStore:
    """Redis behind the SessionStore protocol.

    The URL arrives as an argument rather than from settings: this package is imported by
    four services, and a library that reaches into one service's configuration cannot be
    used by the others.
    """

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("redis url is empty; refusing to build a session store")
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def get(self, token_hash: str) -> tuple[str, str] | None:
        raw = await self._redis.get(_PREFIX + token_hash)
        if raw is None:
            return None
        record = json.loads(raw)
        return record["user_id"], record["role"]

    async def put(self, token_hash: str, user_id: str, role: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return

        record = json.dumps({"user_id": user_id, "role": role})
        # The set of a user's live tokens is kept alongside, so logging out everywhere can
        # reach sessions issued by a different process. Without it, "log out everywhere"
        # would only reach whatever this instance happened to remember.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(_PREFIX + token_hash, record, ex=ttl_seconds)
            pipe.sadd(_USER_PREFIX + user_id, token_hash)
            pipe.expire(_USER_PREFIX + user_id, ttl_seconds)
            await pipe.execute()

    async def delete(self, token_hash: str) -> None:
        await self._redis.delete(_PREFIX + token_hash)

    async def delete_user(self, user_id: str) -> None:
        hashes = await self._redis.smembers(_USER_PREFIX + user_id)
        if hashes:
            await self._redis.delete(*(_PREFIX + h for h in hashes))
        await self._redis.delete(_USER_PREFIX + user_id)


class FakeSessionStore:
    """In-memory store with the same semantics, for tests.

    Expiry is modelled rather than ignored: the TTL is the ceiling on how long a revoked
    session survives a missed delete, so a fake that never expired anything would hide the
    one property that bound matters for.
    """

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, str, float]] = {}

    async def get(self, token_hash: str) -> tuple[str, str] | None:
        record = self._records.get(token_hash)
        if record is None:
            return None

        user_id, role, expires_at = record
        if expires_at <= time.monotonic():
            del self._records[token_hash]
            return None
        return user_id, role

    async def put(self, token_hash: str, user_id: str, role: str, ttl_seconds: int) -> None:
        self._records[token_hash] = (user_id, role, time.monotonic() + ttl_seconds)

    async def delete(self, token_hash: str) -> None:
        self._records.pop(token_hash, None)

    async def delete_user(self, user_id: str) -> None:
        for token_hash in [h for h, r in self._records.items() if r[0] == user_id]:
            del self._records[token_hash]
