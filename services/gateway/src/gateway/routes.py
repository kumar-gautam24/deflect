"""The route table.

This module is the artifact a reviewer reads to answer "what is exposed, and to whom".
It deliberately imports nothing but the standard library: the table should be legible
without following an import into an HTTP client or a settings object.

A path that is absent is unroutable, which is a stronger guarantee than a path that is
guarded. /metrics and the interactive docs are absent for exactly that reason.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    """One public path, and everything the gateway needs to know to serve it.

    `principal` is the requirement a caller must satisfy, resolved by
    deflect_common.auth; None means public. `limit` names a limiter in policy.py rather
    than holding numbers, so the table stays about routing and the policy module stays
    about values.
    """

    method: str
    path: str
    upstream: str
    principal: str | None = None
    timeout: float = 15.0
    stream: bool = False
    limit: str | None = None


ROUTES: tuple[Route, ...] = (
    Route("POST", "/ask", "answer", None, timeout=30, stream=True, limit="ask"),
    Route("POST", "/auth/login", "auth", None, timeout=10, limit="login"),
    Route("POST", "/auth/logout", "auth", "session", timeout=10),
    Route("POST", "/auth/logout-all", "auth", "session", timeout=10),
    Route("GET", "/auth/me", "auth", "session", timeout=10),
    Route("GET", "/traces", "answer", "viewer"),
    Route("GET", "/traces/{trace_id}", "answer", "viewer"),
    Route("POST", "/search", "retrieval", "service"),
    Route("GET", "/documents", "retrieval", "service"),
    Route("POST", "/ingest", "retrieval", "operator"),
    Route("GET", "/jobs/{job_id}", "retrieval", "operator"),
    Route("GET", "/jobs/{job_id}/events", "retrieval", "operator", timeout=30, stream=True),
    Route("POST", "/runs", "evals", "operator"),
    Route("GET", "/eval-runs", "evals", None),
    Route("GET", "/eval-runs/diff", "evals", None),
    Route("GET", "/eval-runs/{run_id}", "evals", None),
    Route("GET", "/eval-runs/{run_id}/events", "evals", None, timeout=30, stream=True),
)
