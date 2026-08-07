"""Abuse control shared by every endpoint that is reachable without a credential.

It began in the answer service when only /ask needed it. Login makes it the second caller,
and what two services need lives here -- an unauthenticated endpoint that performs an
argon2id hash is a denial-of-service target exactly as one that calls a model is.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import Request


class SlidingWindowLimiter:
    """Allow `limit` events per key per `window_seconds`.

    In-memory and per-process, so the allowance resets on restart and each instance
    counts separately. Documented rather than solved: making a throttle survive a
    restart means running Redis, and the control that actually bounds cost -- the daily
    cap -- already survives because it counts rows in the database.

    `now` is a parameter rather than a call to the clock so window expiry is tested
    without sleeping.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, now: float) -> bool:
        """Record an event and report whether it was within the allowance."""
        self._evict(now)

        events = self._events[key]
        if len(events) >= self._limit:
            return False

        events.append(now)
        return True

    def tracked_keys(self, now: float) -> int:
        """How many keys still hold unexpired events. Exposed so a test can prove the
        dict does not grow without bound on a public endpoint."""
        self._evict(now)
        return len(self._events)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        for key in list(self._events):
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            # Drop the key entirely, not just its events: one entry per address ever
            # seen would be an unbounded leak on an endpoint open to the internet.
            if not events:
                del self._events[key]


def client_address(request: Request, trust_forwarded: bool) -> str:
    """The address to rate limit on.

    A forwarded address is trusted only from a caller that presented the service token.
    The web BFF sees the real visitor and forwards it; an anonymous caller reaching this
    service directly could otherwise mint a fresh address per request and make the
    per-address limit meaningless.
    """
    if trust_forwarded:
        forwarded = request.headers.get("x-forwarded-for", "")
        # Leftmost is the originating client; the rest are proxy hops.
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    return request.client.host if request.client else "unknown"


def seconds_until_utc_midnight(now: datetime) -> int:
    """How long until the daily allowance resets, for a Retry-After header."""
    midnight = (now.astimezone(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight - now.astimezone(UTC)).total_seconds())


def edge_address(request: Request, trusted_hops: int = 1) -> str:
    """The client address, for a service that IS the edge.

    `client_address` takes the leftmost X-Forwarded-For entry, which is right when the only
    forwarder is trusted and overwrites the header -- the web BFF does exactly that. It is
    wrong here. A load balancer in front of a public edge APPENDS the real client to
    whatever the caller sent, so the leftmost entry is attacker-supplied and the rightmost
    is the one our own proxy wrote.

    Two functions rather than another boolean on one: the rules are genuinely different,
    and a flag would invite a future caller to pick the wrong one and never find out.

    `trusted_hops` is explicit because the right entry is n-from-the-right. A deployment
    behind two proxies needs a different number, not different code. If the header is too
    short to hold that many, it is not evidence of anything and the peer is used instead.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]

    if len(entries) >= trusted_hops >= 1:
        return entries[-trusted_hops]

    return request.client.host if request.client else "unknown"


@dataclass(frozen=True)
class Decision:
    """Whether the request may proceed, and if not, when to come back.

    retry_after is computed rather than assumed. The sliding-window log this replaces
    could not produce it, which is why both services hardcoded a full hour and told a
    caller one second over the limit to wait sixty minutes.
    """

    allowed: bool
    retry_after: float


class Limiter(Protocol):
    async def check(self, key: str, now: float) -> Decision: ...


class _LeakyBucket:
    """The arithmetic, shared by both implementations so they cannot disagree.

    A bucket of `capacity` units with a hole in it. Every request pours one unit in; the
    hole drains `rate` units per `period_seconds`, continuously. If a request would
    overflow the bucket it is refused, and how long until it fits is division rather than
    a guess -- which is the whole reason the wait can finally be honest.

    Nothing is stored per request, only the level and when it was last observed, so the
    drain is computed on read rather than by a timer. Two floats per key regardless of
    how much traffic an address sends.

    (The same algorithm is often written as GCRA, which tracks the next permitted arrival
    time and needs one float instead of two. It is exactly equivalent -- level is
    (tat - now) / emission -- and it is not used here because a level that drains is
    something a reader can picture and a "theoretical arrival time" is not.)
    """

    def __init__(self, rate: int, period_seconds: float, capacity: int) -> None:
        if rate <= 0 or period_seconds <= 0 or capacity < 1:
            raise ValueError(
                f"a bucket of {capacity} draining {rate} per {period_seconds}s permits "
                "nothing; refusing to build it"
            )
        self._leak_per_second = rate / period_seconds
        self._capacity = float(capacity)

    def _decide(
        self, level: float | None, last_seen: float, now: float
    ) -> tuple[bool, float, float]:
        """Returns (allowed, new_level, retry_after). Pure, so both stores share it."""
        if level is None:
            drained = 0.0
        else:
            # Clamped at zero: an idle bucket is empty, not negative. Without the clamp a
            # long silence would bank credit and the next burst would be unbounded.
            drained = max(0.0, level - (now - last_seen) * self._leak_per_second)

        if drained + 1.0 > self._capacity:
            return False, drained, (drained + 1.0 - self._capacity) / self._leak_per_second

        return True, drained + 1.0, 0.0


class InMemoryLeakyBucket(_LeakyBucket):
    """Per-process, for tests and for a single worker.

    Kept alongside the Redis one for the same reason FakeSessionStore is kept: a test
    suite that needs Redis to run is a test suite that stops being run.
    """

    def __init__(self, rate: int, period_seconds: float, capacity: int) -> None:
        super().__init__(rate, period_seconds, capacity)
        self._buckets: dict[str, tuple[float, float]] = {}

    async def check(self, key: str, now: float) -> Decision:
        held = self._buckets.get(key)
        level, last_seen = held if held is not None else (None, now)
        allowed, new_level, retry_after = self._decide(level, last_seen, now)

        self._buckets[key] = (new_level, now)
        self._evict(now)
        return Decision(allowed, retry_after)

    def _evict(self, now: float) -> None:
        """Drop buckets that have fully drained.

        One entry per address ever seen would be an unbounded leak on an endpoint open to
        the internet -- the same reasoning the sliding window's eviction had.
        """
        empty = [
            key
            for key, (level, last_seen) in self._buckets.items()
            if level - (now - last_seen) * self._leak_per_second <= 0.0
        ]
        for key in empty:
            del self._buckets[key]

    def tracked_keys(self, now: float) -> int:
        self._evict(now)
        return len(self._buckets)


# One round trip, and atomic: a read-modify-write across two calls would let two workers
# both see room in the same bucket and both pour into it.
_LEAK_LUA = """
local held = redis.call('HMGET', KEYS[1], 'level', 'seen')
local now = tonumber(ARGV[1])
local leak = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])

local level = 0.0
if held[1] then
  local drained = tonumber(held[1]) - (now - tonumber(held[2])) * leak
  if drained > 0 then level = drained end
end

if level + 1.0 > capacity then
  return {0, tostring((level + 1.0 - capacity) / leak)}
end

redis.call('HSET', KEYS[1], 'level', tostring(level + 1.0), 'seen', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(((level + 1.0) / leak) * 1000))
return {1, '0'}
"""


class RedisLeakyBucket(_LeakyBucket):
    """The same bucket, shared across workers.

    Values cross the Lua boundary as strings because Redis coerces Lua numbers to
    integers, which would silently truncate every fractional second -- and this algorithm
    is entirely fractional seconds.

    The key's expiry is set to how long the bucket needs to drain completely, so an
    address that stops calling stops costing memory without a sweeper.
    """

    def __init__(
        self,
        redis,
        rate: int,
        period_seconds: float,
        capacity: int,
        prefix: str = "ratelimit:",
    ) -> None:
        super().__init__(rate, period_seconds, capacity)
        self._redis = redis
        self._prefix = prefix
        self._script = redis.register_script(_LEAK_LUA)

    async def check(self, key: str, now: float) -> Decision:
        allowed, retry_after = await self._script(
            keys=[self._prefix + key],
            args=[now, self._leak_per_second, self._capacity],
        )
        return Decision(bool(int(allowed)), float(retry_after))
