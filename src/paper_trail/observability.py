"""Structured logging: one JSON object per line on stdout (PLAN 1.4).

structlog renders both its own events and stdlib records (uvicorn, alembic, httpx), so every
line has ``timestamp``, ``level``, ``event`` and ``logger`` plus any bound context such as
``request_id`` or ``receipt_id``.
"""

import logging
import sys
from typing import TextIO

import structlog
from structlog.typing import Processor

from paper_trail.config import Settings

_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "alembic", "httpx")


class _StdoutHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever ``sys.stdout`` is at emit time, so redirection is always honoured."""

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        """The stream is always the current ``sys.stdout``."""


class _StderrHandler(_StdoutHandler):
    """Like ``_StdoutHandler``, for ``sys.stderr``."""

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        """The stream is always the current ``sys.stderr``."""


def configure_logging(settings: Settings, *, to_stderr: bool = False) -> None:
    """Route structlog and stdlib logging to stdout, or to stderr for commands whose stdout is
    their answer (the agent CLI's trace). Safe to call more than once."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: list[Processor] = (
        [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
        if settings.log_format == "json"
        else [structlog.dev.ConsoleRenderer(colors=False)]
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = _StderrHandler() if to_stderr else _StdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level)
    for name in _THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
