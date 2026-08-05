import json
import logging

from deflect_common.logging import JSONFormatter, request_id


def _format(record_kwargs: dict | None = None) -> dict:
    record = logging.LogRecord(
        name="deflect", level=logging.INFO, pathname="p", lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    for key, value in (record_kwargs or {}).items():
        setattr(record, key, value)
    return json.loads(JSONFormatter().format(record))


def test_a_log_line_is_parseable_json():
    assert _format()["message"] == "hello"


def test_the_line_carries_level_and_logger_name():
    line = _format()
    assert line["level"] == "INFO"
    assert line["logger"] == "deflect"


def test_the_request_id_is_included_when_set():
    """Without this the three-service hop cannot be reassembled from logs at all."""
    token = request_id.set("req-123")
    try:
        assert _format()["request_id"] == "req-123"
    finally:
        request_id.reset(token)


def test_the_field_is_absent_rather_than_null_when_unset():
    assert "request_id" not in _format()


def test_an_exception_is_rendered_into_the_line():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="d", level=logging.ERROR, pathname="p", lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
        line = json.loads(JSONFormatter().format(record))

    assert "ValueError: boom" in line["exception"]


async def test_requests_are_counted_and_timed_by_route_template():
    """Labelled by template, not raw path: /traces/17 and /traces/18 are one operation,
    and a series per id would be an unbounded cardinality leak."""
    import httpx
    from fastapi import FastAPI
    from prometheus_client import generate_latest

    from deflect_common.observability import RequestIdMiddleware

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/traces/{trace_id}")
    async def one(trace_id: int) -> dict:
        return {"id": trace_id}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/traces/17")
        await client.get("/traces/18")

    exposition = generate_latest().decode()

    assert 'route="/traces/{trace_id}"' in exposition
    assert 'route="/traces/17"' not in exposition


async def test_an_unmatched_path_does_not_mint_a_series_per_url():
    """A 404 scan would otherwise create one time series per probed path."""
    import httpx
    from fastapi import FastAPI
    from prometheus_client import generate_latest

    from deflect_common.observability import RequestIdMiddleware

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/nope-a")
        await client.get("/nope-b")

    exposition = generate_latest().decode()

    assert 'route="unmatched"' in exposition
    assert "nope-a" not in exposition
