"""Per-request correlation id, access log and last-resort error boundary (PLAN 1.4); the
request body size limit; the Host and Origin checks (ADR-0020)."""

import ipaddress
import logging
import re
import time
from collections.abc import Iterable
from http import HTTPStatus
from uuid import uuid4

import structlog
from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from paper_trail.interfaces.web.errors import (
    internal_error_response,
    problem_response,
    too_large_detail,
)

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_BRACKETED_HOST = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::[0-9]+)?")
_NAMED_HOST = re.compile(r"([A-Za-z0-9.-]+)(?::[0-9]+)?")
_DEFAULT_PORTS = {"http": 80, "https": 443}
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


class BodySizeLimitMiddleware:
    """Refuses a request body above ``max_bytes`` with a 413 problem: before reading it when
    its Content-Length says so, else as soon as more has arrived, so an oversized upload is
    never spooled whole."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > self.max_bytes:
            response = problem_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                detail=too_large_detail(self.max_bytes),
                instance=scope["path"],
            )
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE, too_large_detail(self.max_bytes)
                    )
            return message

        await self.app(scope, limited_receive, send)


class HostOriginMiddleware:
    """The app has no login, so these two checks are its boundary (ADR-0020).

    A request whose ``Host`` (without its port) is not one of ``allowed_hosts`` is refused with
    a 400 problem: a page on another site cannot reach the app by DNS rebinding. A request with
    a method that changes data and an ``Origin`` header that is not the request's own origin
    (its scheme, ``Host`` and port) is refused with a 403 problem: another site's page cannot
    post to it. A request without ``Origin`` (curl, the CLI) and a safe method are served.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed_hosts = frozenset(
            canonical_ipv6(host) or host.lower() for host in allowed_hosts
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if _hostname(host) not in self.allowed_hosts:
            response = problem_response(
                HTTPStatus.BAD_REQUEST,
                detail="This host name is not served here.",
                instance=scope["path"],
            )
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        own_origin = f"{scope['scheme']}://{host}".lower()
        if (
            scope["method"] not in _SAFE_METHODS
            and origin is not None
            and _origin(origin) != _origin(own_origin)
        ):
            response = problem_response(
                HTTPStatus.FORBIDDEN,
                detail="Requests from another site are not accepted.",
                instance=scope["path"],
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _hostname(host: str) -> str | None:
    """A ``Host`` header's name without its port: lowercased, an IPv6 address in its
    compressed form without brackets. None for anything else (a stray bracket or colon, a
    port that is not digits)."""
    bracketed = _BRACKETED_HOST.fullmatch(host)
    if bracketed is not None:
        return canonical_ipv6(bracketed[1])
    named = _NAMED_HOST.fullmatch(host)
    return None if named is None else named[1].lower()


def canonical_ipv6(address: str) -> str | None:
    """``address`` as a compressed IPv6 address, or None when it is not one."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    return parsed.compressed if isinstance(parsed, ipaddress.IPv6Address) else None


def _origin(origin: str) -> str:
    """``scheme://host`` with the scheme's default port dropped, lowercased."""
    origin = origin.lower()
    for scheme, port in _DEFAULT_PORTS.items():
        if origin.startswith(f"{scheme}://") and origin.endswith(f":{port}"):
            return origin.removesuffix(f":{port}")
    return origin
