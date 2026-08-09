import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
import redis.exceptions
from fastapi import Request

from deflect_common.ratelimit import (
    Decision,
    InMemoryLeakyBucket,
    RedisLeakyBucket,
    SlidingWindowLimiter,
    client_address,
    edge_address,
    seconds_until_utc_midnight,
)


def test_requests_up_to_the_limit_are_allowed():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)

    assert [limiter.check("ip", now=0.0) for _ in range(3)] == [True, True, True]


def test_the_request_past_the_limit_is_rejected():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("ip", now=0.0)
    limiter.check("ip", now=0.0)

    assert limiter.check("ip", now=0.0) is False


def test_the_allowance_returns_once_the_window_passes():
    """Driven by the injected clock, so the test does not sleep for a minute."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("ip", now=0.0)

    assert limiter.check("ip", now=59.0) is False
    assert limiter.check("ip", now=61.0) is True


def test_the_window_slides_rather_than_resetting_on_a_boundary():
    """A fixed bucket would allow 2x the limit across a boundary. This must not."""
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("ip", now=0.0)
    limiter.check("ip", now=59.0)

    assert limiter.check("ip", now=59.5) is False
    assert limiter.check("ip", now=60.5) is True   # the 0.0 entry has aged out
    assert limiter.check("ip", now=60.6) is False  # the 59.0 entry has not


def test_addresses_are_counted_separately():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("first", now=0.0)

    assert limiter.check("second", now=0.0) is True


def test_expired_entries_do_not_accumulate_forever():
    """Without eviction this dict is an unbounded memory leak on a public endpoint."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    for i in range(500):
        limiter.check(f"ip-{i}", now=float(i))

    assert limiter.tracked_keys(now=10_000.0) == 0


class FakeRequest:
    def __init__(self, headers: dict, host: str | None):
        self.headers = headers
        self.client = type("C", (), {"host": host})() if host else None


def test_an_authenticated_caller_is_trusted_about_the_forwarded_address():
    request = FakeRequest({"x-forwarded-for": "203.0.113.7, 70.41.3.18"}, "10.0.0.1")

    assert client_address(request, trust_forwarded=True) == "203.0.113.7"


def test_an_anonymous_caller_is_limited_on_its_own_socket_address():
    """Otherwise a direct caller mints a fresh address per request and evades the limit."""
    request = FakeRequest({"x-forwarded-for": "1.2.3.4"}, "10.0.0.1")

    assert client_address(request, trust_forwarded=False) == "10.0.0.1"


def test_a_trusted_caller_sending_no_forwarded_header_falls_back_to_the_socket():
    request = FakeRequest({}, "10.0.0.1")

    assert client_address(request, trust_forwarded=True) == "10.0.0.1"


def test_a_request_with_no_client_is_still_keyable():
    request = FakeRequest({}, None)

    assert client_address(request, trust_forwarded=False) == "unknown"


def test_seconds_until_midnight_counts_down_within_the_day():
    now = datetime(2026, 8, 5, 23, 59, 30, tzinfo=UTC)

    assert seconds_until_utc_midnight(now) == 30


def test_seconds_until_midnight_is_a_full_day_at_midnight():
    now = datetime(2026, 8, 5, 0, 0, 0, tzinfo=UTC)

    assert seconds_until_utc_midnight(now) == 86_400


def _request(headers: dict[str, str], peer: str | None = "10.0.0.1") -> Request:
    """A Starlette request with the given headers and peer, without a server."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def test_the_edge_takes_the_entry_its_own_proxy_appended():
    """Render appends the real client to whatever the caller sent, so the LAST entry is
    the only one the gateway's own proxy wrote."""
    request = _request({"X-Forwarded-For": "203.0.113.7"})

    assert edge_address(request) == "203.0.113.7"


def test_a_spoofed_leading_entry_is_ignored():
    """The whole point. A caller who sends their own X-Forwarded-For would otherwise mint
    a fresh rate-limit key per request and make the limit decorative."""
    request = _request({"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})

    assert edge_address(request) == "203.0.113.7"


def test_many_spoofed_entries_are_still_ignored():
    request = _request({"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.7"})

    assert edge_address(request) == "203.0.113.7"


def test_two_trusted_hops_takes_the_second_from_the_right():
    """A deployment behind two proxies needs a different number, not different code."""
    request = _request({"X-Forwarded-For": "9.9.9.9, 203.0.113.7, 10.0.0.9"})

    assert edge_address(request, trusted_hops=2) == "203.0.113.7"


def test_no_forwarded_header_falls_back_to_the_peer():
    """Direct connections happen in development, and must not all share one key."""
    request = _request({}, peer="192.168.1.5")

    assert edge_address(request) == "192.168.1.5"


def test_fewer_entries_than_trusted_hops_falls_back_to_the_peer():
    """A header too short to contain a trusted entry is not evidence of anything, so it
    must not be believed."""
    request = _request({"X-Forwarded-For": "9.9.9.9"}, peer="192.168.1.5")

    assert edge_address(request, trusted_hops=2) == "192.168.1.5"


def test_no_peer_and_no_header_is_a_single_known_bucket():
    request = _request({}, peer=None)

    assert edge_address(request) == "unknown"


def test_whitespace_and_empty_entries_do_not_shift_the_answer():
    request = _request({"X-Forwarded-For": "9.9.9.9 , , 203.0.113.7 "})

    assert edge_address(request) == "203.0.113.7"


RATE, PERIOD, CAPACITY = 20, 3600.0, 5
DRAIN = PERIOD / RATE  # 180 seconds to leak one unit


def _limiter() -> InMemoryLeakyBucket:
    return InMemoryLeakyBucket(rate=RATE, period_seconds=PERIOD, capacity=CAPACITY)


async def _fill(limiter: InMemoryLeakyBucket, now: float = 0.0) -> None:
    for _ in range(CAPACITY):
        await limiter.check("a", now=now)


async def test_a_full_bucket_can_be_filled_back_to_back():
    """Five questions in a row is someone trying the demo, not abusing it."""
    limiter = _limiter()

    for _ in range(CAPACITY):
        assert (await limiter.check("a", now=0.0)).allowed is True


async def test_the_request_that_would_overflow_is_refused():
    limiter = _limiter()
    await _fill(limiter)

    assert (await limiter.check("a", now=0.0)).allowed is False


async def test_the_refusal_says_how_long_until_one_unit_has_leaked():
    limiter = _limiter()
    await _fill(limiter)

    decision = await limiter.check("a", now=0.0)

    assert decision.retry_after == pytest.approx(DRAIN)


async def test_waiting_the_advertised_time_lets_exactly_one_through():
    """The test the hardcoded Retry-After: 3600 could never have passed."""
    limiter = _limiter()
    await _fill(limiter)
    refused = await limiter.check("a", now=0.0)

    assert (await limiter.check("a", now=refused.retry_after)).allowed is True
    assert (await limiter.check("a", now=refused.retry_after)).allowed is False


async def test_it_keeps_leaking_at_the_sustained_rate():
    """Twenty an hour, whatever the capacity. One request every drain interval is
    accepted indefinitely, because that is exactly the rate the hole permits."""
    limiter = _limiter()
    await _fill(limiter)

    for step in range(1, RATE + 1):
        assert (await limiter.check("a", now=step * DRAIN)).allowed is True


async def test_a_long_silence_does_not_bank_credit():
    """An idle bucket is empty, not negative. Without the clamp, an address that went
    quiet for a day would come back able to send an unbounded burst."""
    limiter = _limiter()
    await _fill(limiter)

    for _ in range(CAPACITY):
        assert (await limiter.check("a", now=PERIOD * 24)).allowed is True

    assert (await limiter.check("a", now=PERIOD * 24)).allowed is False


async def test_keys_do_not_share_a_bucket():
    limiter = _limiter()
    await _fill(limiter)

    assert (await limiter.check("b", now=0.0)).allowed is True


async def test_an_allowed_decision_asks_for_no_wait():
    assert (await _limiter().check("a", now=0.0)) == Decision(allowed=True, retry_after=0.0)


async def test_a_capacity_of_one_is_a_plain_rate_limit():
    limiter = InMemoryLeakyBucket(rate=RATE, period_seconds=PERIOD, capacity=1)

    assert (await limiter.check("a", now=0.0)).allowed is True
    assert (await limiter.check("a", now=0.0)).allowed is False


async def test_a_drained_bucket_is_forgotten():
    """One entry per address ever seen would be an unbounded leak on a public endpoint."""
    limiter = _limiter()
    await limiter.check("a", now=0.0)

    assert limiter.tracked_keys(now=0.0) == 1
    assert limiter.tracked_keys(now=DRAIN * CAPACITY * 2) == 0


async def test_a_nonsense_configuration_is_refused_at_construction():
    """A limiter that permits nothing is a misconfiguration, and it should fail where it
    is built rather than on the first request."""
    for kwargs in [
        {"rate": 0, "period_seconds": 60.0, "capacity": 1},
        {"rate": 10, "period_seconds": 0.0, "capacity": 1},
        {"rate": 10, "period_seconds": 60.0, "capacity": 0},
    ]:
        with pytest.raises(ValueError):
            InMemoryLeakyBucket(**kwargs)


# A distinct prefix so this test cannot collide with a running service's keys, and so
# cleanup can target exactly what it wrote -- never FLUSHDB, which would take the evals
# and retrieval consumer groups down with it.
#
# The URL is an env var, defaulting to the local docker-compose instance, because CI's
# Redis lives at the same host and port but is a *different* container -- see the
# contracts job in ci.yml.
_REDIS_URL = os.environ.get(
    "RATELIMIT_TEST_REDIS_URL", "redis://:dev-redis-password@localhost:6379/0"
)
_AGREEMENT_PREFIX = "ratelimit-test:leaky-bucket-agreement:"


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except redis.exceptions.RedisError as cause:
        # Skipping keeps the unit suite runnable without Redis for local development,
        # but a check that can silently vanish is worthless -- which is exactly what
        # happened: this test only ran when someone had Redis up locally, the contracts
        # job never gave it one, and "1 skipped" went unnoticed on every push. CI sets
        # REQUIRE_RATELIMIT_REDIS_CHECK so an unreachable Redis fails the build there
        # instead, the same pattern REQUIRE_CORPUS_CHECK uses in evals/test_dataset.py.
        if os.environ.get("REQUIRE_RATELIMIT_REDIS_CHECK"):
            raise AssertionError(
                f"redis required but unreachable at {_REDIS_URL}: {cause}"
            ) from cause
        pytest.skip(f"redis not reachable at {_REDIS_URL}: {cause}")

    await client.delete(_AGREEMENT_PREFIX + "a")  # a prior failed run may have left it
    yield client
    await client.delete(_AGREEMENT_PREFIX + "a")
    await client.aclose()


async def test_redis_and_in_memory_buckets_agree(redis_client):
    """_LEAK_LUA is a hand-written copy of _decide in another language -- Lua cannot call
    the Python method, so the two bodies can only be kept honest by comparison, not by
    sharing code. Driving both through an identical sequence of calls and diffing every
    decision is that comparison; it is what would catch a clamp or an overflow comparison
    edited in one language and not the other.
    """
    memory = InMemoryLeakyBucket(rate=RATE, period_seconds=PERIOD, capacity=CAPACITY)
    on_redis = RedisLeakyBucket(
        redis_client, rate=RATE, period_seconds=PERIOD, capacity=CAPACITY, prefix=_AGREEMENT_PREFIX
    )

    # Fill to capacity, hit the overflow point, walk forward through several sustained
    # drain steps, then jump a long silence to exercise the clamp, and overflow again.
    # The overflow point and a drain step are exactly where a `>` vs `>=` or a dropped
    # tostring would show up as a mismatch between the two implementations.
    now_values = (
        [0.0] * CAPACITY
        + [0.0]
        + [DRAIN * i for i in range(1, RATE + 1)]
        + [PERIOD * 24]
        + [PERIOD * 24] * (CAPACITY - 1)
        + [PERIOD * 24]
    )

    for now in now_values:
        mem_decision = await memory.check("a", now=now)
        redis_decision = await on_redis.check("a", now=now)

        assert mem_decision.allowed == redis_decision.allowed
        assert mem_decision.retry_after == pytest.approx(redis_decision.retry_after)
