"""
Structured JSON logger.
All log records include: timestamp, level, logger name, message, and any
extra fields passed via the `extra` keyword argument.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """Render every log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }

        # Attach any extra fields the caller passed via extra={...}
        _reserved = logging.LogRecord.__dict__.keys() | {
            "message", "asctime", "args", "exc_info", "exc_text",
            "stack_info", "taskName",
        }
        for k, v in record.__dict__.items():
            if k not in _reserved and not k.startswith("_"):
                payload[k] = v

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that writes JSON to stdout."""
    log = logging.getLogger(name)

    if log.handlers:
        return log  # already configured

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    log.addHandler(handler)
    log.propagate = False

    return log
