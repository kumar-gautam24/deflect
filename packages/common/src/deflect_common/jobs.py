"""Job transport shared by every service that enqueues work.

Redis carries work; Postgres carries truth. The envelope holds only a job id, so a message
can never disagree with the row it refers to, and losing the Redis volume costs in-flight
delivery rather than history.

The queue sits behind a Protocol with an in-memory fake so the whole suite runs without
Redis, the same way FakeRetrieval and FakeAnswer already stand in for services.
"""

from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

INGEST_STREAM = "deflect:ingest"
EVAL_ITEM_STREAM = "deflect:eval-items"

# One group per stream. A second worker joins the same group and shares the work rather
# than receiving its own copy, which is the difference between scaling out and duplicating.
CONSUMER_GROUP = "workers"

_FIELD = "job_id"


@dataclass(frozen=True)
class Delivery:
    """A claimed message: the job id, and the handle needed to acknowledge it."""

    message_id: str
    job_id: int


class JobQueue(Protocol):
    async def ensure_group(self, stream: str) -> None: ...

    async def enqueue(self, stream: str, job_id: int) -> None: ...

    async def claim(self, stream: str, consumer: str, count: int) -> list[Delivery]: ...

    async def acknowledge(self, stream: str, message_id: str) -> None: ...

    async def reclaim_stale(
        self, stream: str, consumer: str, min_idle_ms: int
    ) -> list[Delivery]: ...


def _check_id(job_id: int) -> None:
    # bool is an int subclass and would enqueue True as job 1.
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise TypeError(f"job id must be an int, got {type(job_id).__name__}")


class RedisJobQueue:
    """Redis Streams behind the JobQueue protocol.

    The URL arrives as an argument rather than from settings: this package is imported by
    three services, and a library that reaches into one service's configuration cannot be
    used by the others.
    """

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("redis url is empty; refusing to build a queue that cannot connect")
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def ensure_group(self, stream: str) -> None:
        """Create the consumer group, tolerating one that already exists.

        MKSTREAM matters: without it the first worker to start before any producer fails
        on a stream that does not exist yet, which is the normal order on a cold deploy.
        """
        try:
            await self._redis.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
        except ResponseError as cause:
            if "BUSYGROUP" not in str(cause):
                raise

    async def enqueue(self, stream: str, job_id: int) -> None:
        _check_id(job_id)
        await self._redis.xadd(stream, {_FIELD: job_id})

    async def claim(self, stream: str, consumer: str, count: int) -> list[Delivery]:
        response = await self._redis.xreadgroup(
            CONSUMER_GROUP, consumer, {stream: ">"}, count=count, block=2000
        )
        return [
            Delivery(message_id, int(fields[_FIELD]))
            for _, entries in response or []
            for message_id, fields in entries
        ]

    async def acknowledge(self, stream: str, message_id: str) -> None:
        await self._redis.xack(stream, CONSUMER_GROUP, message_id)

    async def reclaim_stale(
        self, stream: str, consumer: str, min_idle_ms: int
    ) -> list[Delivery]:
        _, entries, _ = await self._redis.xautoclaim(
            stream, CONSUMER_GROUP, consumer, min_idle_time=min_idle_ms, count=10
        )
        return [Delivery(message_id, int(fields[_FIELD])) for message_id, fields in entries]


class FakeJobQueue:
    """In-memory queue with the same delivery semantics, for tests.

    It models the one property that matters and is easy to get wrong: a claimed message
    stays pending until acknowledged, so a crash redelivers rather than losing the job.
    """

    def __init__(self) -> None:
        self._ready: dict[str, list[Delivery]] = {}
        self._pending: dict[str, list[tuple[Delivery, int]]] = {}
        self._groups: set[str] = set()
        self._next = 0

    async def ensure_group(self, stream: str) -> None:
        self._groups.add(stream)

    async def enqueue(self, stream: str, job_id: int) -> None:
        _check_id(job_id)
        self._next += 1
        self._ready.setdefault(stream, []).append(Delivery(f"{self._next}-0", job_id))

    async def claim(self, stream: str, consumer: str, count: int) -> list[Delivery]:
        # Real Redis answers NOGROUP if XGROUP CREATE never ran, so a worker that forgets
        # ensure_group would pass every test against a permissive fake and then crash on
        # its first message in production. The fake models the failure, not just the
        # success -- a fake that only agrees where the real thing agrees is a trap.
        if stream not in self._groups:
            raise RuntimeError(f"no consumer group on {stream}; call ensure_group first")

        ready = self._ready.get(stream, [])
        taken, self._ready[stream] = ready[:count], ready[count:]
        self._pending.setdefault(stream, []).extend((d, 0) for d in taken)
        return taken

    async def acknowledge(self, stream: str, message_id: str) -> None:
        self._pending[stream] = [
            entry for entry in self._pending.get(stream, []) if entry[0].message_id != message_id
        ]

    async def reclaim_stale(
        self, stream: str, consumer: str, min_idle_ms: int
    ) -> list[Delivery]:
        # Idle time is zero in the fake, so a positive threshold reclaims nothing. That is
        # what lets a test assert a fresh job is left alone.
        if min_idle_ms > 0:
            return []
        return [delivery for delivery, _ in self._pending.get(stream, [])]

    async def pending_count(self, stream: str) -> int:
        return len(self._pending.get(stream, []))
