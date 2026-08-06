"""Session storage shared by every service that checks one.

Postgres is the record and this is the working copy. Services read here rather than
querying the auth database, so no service depends on auth being reachable to serve its own
data -- you simply cannot log in while it is down.

An entry is written with the whole remaining lifetime of the session it stands for, which
makes revocation the job of the explicit delete on logout rather than of expiry. A shorter
TTL is tempting as a backstop, but since this is the only place a service looks, it would
not bound revocation -- it would quietly become the session length, ending sessions whose
cookie and database row both still called them live.

The cost is that a delete which never lands leaves a revoked session usable until it would
have expired anyway. Both writers of that delete run on the same code path as the database
write, so the gap is a Redis failure rather than a logic error.
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

        try:
            record = json.loads(raw)
            return record["user_id"], record["role"]
        except (ValueError, TypeError, KeyError):
            # A value we cannot read is one session that fails to resolve, not an outage.
            # Letting it raise would escape the guard dependency and 500 every request
            # presenting a session, on all four services at once -- the same reasoning
            # that made verify_password return False rather than raise.
            return None

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

    Expiry is modelled rather than ignored: the TTL is the session's own lifetime, so a
    fake that never expired anything would hide the fact that this store decides when a
    session ends everywhere except the auth service itself.
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
