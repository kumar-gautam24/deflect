"""Failing fast on an upstream that is already failing.

Without this, every gateway worker ends up blocked on the same sick service waiting for
the same timeout, and one service being down becomes the edge being down. The point is
not to protect the upstream -- it is to keep the gateway's workers available for the
three upstreams that are still healthy.
"""

from dataclasses import dataclass, field


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None


@dataclass
class CircuitBreaker:
    """Per-upstream, because upstreams fail independently.

    Counts CONSECUTIVE failures: an upstream that fails once a day is healthy, and a
    cumulative count would eventually open the circuit on a service that never had a
    problem.
    """

    failures: int
    cooldown_seconds: float
    _states: dict[str, _State] = field(default_factory=dict)

    def is_open(self, upstream: str, now: float) -> bool:
        state = self._states.get(upstream)
        if state is None or state.opened_at is None:
            return False

        if now - state.opened_at >= self.cooldown_seconds:
            # Closed optimistically rather than after a probe: the next real request is
            # the probe, and it either succeeds or opens the circuit again.
            state.opened_at = None
            state.consecutive_failures = 0
            return False

        return True

    def record_failure(self, upstream: str, now: float) -> None:
        state = self._states.setdefault(upstream, _State())
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failures:
            state.opened_at = now

    def record_success(self, upstream: str) -> None:
        state = self._states.get(upstream)
        if state is not None:
            state.consecutive_failures = 0
            state.opened_at = None
