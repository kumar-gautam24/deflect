"""Abuse control for the one anonymous endpoint.

This lives in the answer service rather than packages/common because exactly one
service needs it. Three services share authentication; only this one takes public
traffic.

Two layers doing two different jobs. The per-address window stops one script. The
daily cap is the only thing that bounds the provider bill, because a botnet has many
real addresses. Conflating them would leave the deployment believing it is protected
when only half of it is.
"""

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from answer.models import Trace


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


async def questions_today(session: AsyncSession, now: datetime) -> int:
    """How many questions have been answered since UTC midnight.

    Counts trace rows rather than summing cost_usd. Summing would bound the bill more
    directly, but estimate_cost returns 0.0 for any model absent from PRICING, so
    pointing generation_model at an unpriced model would silently turn the cap into no
    cap at all -- a control that fails open on an ordinary configuration change. A row
    count cannot do that.

    No new table and no migration: the answer service already writes one row per
    question, so the day's counter already exists and survives a restart.
    """
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    statement = select(func.count()).select_from(Trace).where(Trace.created_at >= midnight)
    return (await session.execute(statement)).scalar_one()
