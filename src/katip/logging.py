"""Yapılandırılmış loglama."""

from __future__ import annotations

import logging
import sys

import structlog


class AccessQueryFilter(logging.Filter):
    """Keep request paths and status codes without logging bearer query links."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, protocol, status = record.args
            if isinstance(target, str) and "?" in target:
                record.args = (
                    client,
                    method,
                    target.partition("?")[0] + "?[redacted]",
                    protocol,
                    status,
                )
        return True


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessQueryFilter) for f in access.filters):
        access.addFilter(AccessQueryFilter())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
