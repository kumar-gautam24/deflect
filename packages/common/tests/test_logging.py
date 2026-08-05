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
