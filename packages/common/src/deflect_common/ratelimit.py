"""Abuse control shared by every endpoint that is reachable without a credential.

It began in the answer service when only /ask needed it. Login makes it the second caller,
and what two services need lives here -- an unauthenticated endpoint that performs an
argon2id hash is a denial-of-service target exactly as one that calls a model is.
"""

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

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
