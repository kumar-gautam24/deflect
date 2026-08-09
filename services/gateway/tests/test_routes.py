import pytest

from gateway.routes import ROUTES

VALID_PRINCIPALS = {None, "service", "operator", "viewer", "session"}


def test_every_route_names_a_known_principal():
    """A typo here would build a guard nothing satisfies, and the route would 401 for
    everyone with nothing explaining why."""
    unknown = {r.principal for r in ROUTES} - VALID_PRINCIPALS

    assert unknown == set()


def test_every_route_names_a_known_upstream():
    unknown = {r.upstream for r in ROUTES} - {"retrieval", "answer", "evals", "auth"}

    assert unknown == set()


def test_no_route_exposes_metrics():
    """/metrics is unroutable rather than protected: a path absent from the table cannot
    be reached even if a guard is later mis-wired."""
    assert not any("/metrics" in r.path for r in ROUTES)


def test_no_route_exposes_interactive_docs():
    assert not any(r.path.startswith(("/docs", "/redoc", "/openapi")) for r in ROUTES)


def test_no_two_routes_share_a_method_and_path():
    pairs = [(r.method, r.path) for r in ROUTES]

    assert len(pairs) == len(set(pairs))


def test_every_route_has_a_positive_timeout():
    assert all(r.timeout > 0 for r in ROUTES)


@pytest.mark.parametrize(
    ("method", "path", "principal"),
    [
        ("POST", "/ask", None),
        ("POST", "/auth/login", None),
        ("GET", "/auth/me", "session"),
        ("GET", "/traces", "viewer"),
        ("POST", "/search", "service"),
        ("POST", "/ingest", "operator"),
        ("POST", "/runs", "operator"),
        ("GET", "/eval-runs", None),
    ],
)
def test_the_table_matches_the_spec(method, path, principal):
    """Pinned individually so a careless edit to the table fails a named test rather than
    silently changing who can reach what."""
    route = next(r for r in ROUTES if r.method == method and r.path == path)

    assert route.principal == principal


def test_streaming_routes_are_the_ones_that_stream():
    streaming = {(r.method, r.path) for r in ROUTES if r.stream}

    assert streaming == {
        ("POST", "/ask"),
        ("GET", "/jobs/{job_id}/events"),
        ("GET", "/eval-runs/{run_id}/events"),
    }


def test_a_route_is_frozen():
    """The table is data. Mutating it at runtime would make the security posture depend on
    import order."""
    with pytest.raises(AttributeError):
        ROUTES[0].principal = "service"
