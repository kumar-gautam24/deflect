from gateway.breaker import CircuitBreaker
from gateway.policy import Policy

FAILURES = Policy.BREAKER_FAILURES
COOLDOWN = Policy.BREAKER_COOLDOWN_SECONDS


def _breaker() -> CircuitBreaker:
    return CircuitBreaker(FAILURES, COOLDOWN)


def test_a_healthy_upstream_is_never_open():
    assert _breaker().is_open("answer", now=0.0) is False


def test_fewer_failures_than_the_threshold_keeps_it_closed():
    """One blip is not a pattern, and refusing after one would make the gateway flap."""
    breaker = _breaker()
    for _ in range(FAILURES - 1):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=0.0) is False


def test_the_threshold_opens_it():
    breaker = _breaker()
    for _ in range(FAILURES):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=0.0) is True


def test_it_closes_again_after_the_cooldown():
    breaker = _breaker()
    for _ in range(FAILURES):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=COOLDOWN + 1) is False


def test_a_success_resets_the_count():
    """Consecutive failures, not cumulative: an upstream that fails occasionally over
    days is healthy, and treating that as a pattern would open the circuit on nothing."""
    breaker = _breaker()
    for _ in range(FAILURES - 1):
        breaker.record_failure("answer", now=0.0)
    breaker.record_success("answer")
    breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=0.0) is False


def test_one_sick_upstream_does_not_open_another():
    breaker = _breaker()
    for _ in range(FAILURES):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("retrieval", now=0.0) is False
