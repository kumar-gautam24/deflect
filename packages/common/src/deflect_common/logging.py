"""Structured logging shared by every service.

A ~30-line formatter rather than structlog: five services need identical log shape,
which is exactly what this package is for, and one small class is cheaper to own than a
dependency.
"""

import json
import logging
from contextvars import ContextVar

# Request-scoped rather than passed through every call signature. A correlation id
# threaded by hand would have to reach code that has no other reason to know about it.
request_id: ContextVar[str] = ContextVar("request_id", default="")

_BUILTIN = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        current = request_id.get()
        # Absent rather than null when unset: a null would suggest a request that lost
        # its id, which is a different and more alarming thing than a startup log line.
        if current:
            line["request_id"] = current

        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)

        line.update(
            {k: v for k, v in record.__dict__.items() if k not in _BUILTIN and k != "message"}
        )
        return json.dumps(line, default=str)


def configure_logging() -> None:
    """Replace the root handler's formatter. Called once at import by each service."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
