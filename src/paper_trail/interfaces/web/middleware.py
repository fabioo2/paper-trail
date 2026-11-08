"""Per-request correlation id, access log and last-resort error boundary (PLAN 1.4)."""

import logging
import re
import time
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from paper_trail.interfaces.web.errors import internal_error_response

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_QUIET_PATHS = frozenset({"/api/health", "/api/health/ready"})

log = structlog.get_logger(__name__)


class RequestContextMiddleware:
    """Binds ``request_id`` for every log line of a request and echoes it as X-Request-ID.

    A valid incoming id is kept, anything else is replaced by a fresh UUID4 hex. Exceptions
    that escape the app are logged with their traceback and answered with a generic 500
    problem, so the response still carries the id. If the response had already started (a
    streaming body), a second one cannot be sent: the exception is logged and re-raised so the
    server aborts the connection.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid4().hex
        status = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                log.exception("http.unhandled_error", response_started=response_started)
                if response_started:
                    raise
                await internal_error_response()(scope, receive, send_with_request_id)
            finally:
                path: str = scope["path"]
                log.log(
                    logging.DEBUG if path in _QUIET_PATHS else logging.INFO,
                    "http.request",
                    method=scope["method"],
                    path=path,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
