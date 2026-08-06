from datetime import UTC, datetime

from deflect_common.ratelimit import (
    SlidingWindowLimiter,
    client_address,
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
