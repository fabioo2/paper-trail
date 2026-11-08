"""RequestContextMiddleware's error boundary at the ASGI level (S0-AC-16, S0-AC-19)."""

import asyncio
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from paper_trail.config import Settings
from paper_trail.interfaces.web.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from paper_trail.observability import configure_logging
from tests.support import JsonLogs, MakeSettings

FAILURE = "stream broke after the headers"


@pytest.fixture
def logged(make_settings: MakeSettings) -> Settings:
    settings = make_settings(log_level="DEBUG", log_format="json")
    configure_logging(settings)
    return settings


def _scope() -> Scope:
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/_test/stream",
        "headers": [(REQUEST_ID_HEADER.lower().encode(), b"stream-1")],
    }


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _call(app: Any) -> tuple[list[Message], BaseException | None]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    try:
        asyncio.run(RequestContextMiddleware(app)(_scope(), _receive, send))
    except Exception as error:
        return sent, error
    return sent, None


def _starts(sent: list[Message]) -> list[Message]:
    return [m for m in sent if m["type"] == "http.response.start"]


def test_s0_ac_19_error_before_the_response_becomes_a_500_problem(
    logged: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-19: an exception before any byte is sent is answered with one generic 500."""

    async def app(_scope: Scope, _receive: Receive, _send: Send) -> None:
        raise RuntimeError(FAILURE)

    sent, escaped = _call(app)

    assert escaped is None
    (start,) = _starts(sent)
    assert start["status"] == 500
    assert (REQUEST_ID_HEADER.lower().encode(), b"stream-1") in start["headers"]


def test_s0_ac_19_error_after_the_response_started_is_logged_and_reraised(
    logged: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-19: mid-stream failures cannot get a second response start; the original error
    is logged with the request id and re-raised so the server aborts the connection."""

    async def app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        raise RuntimeError(FAILURE)

    sent, escaped = _call(app)

    assert isinstance(escaped, RuntimeError)
    assert str(escaped) == FAILURE
    assert len(_starts(sent)) == 1
    records = json_logs()
    (error,) = [r for r in records if r["event"] == "http.unhandled_error"]
    assert error["request_id"] == "stream-1"
    assert error["response_started"] is True
    assert FAILURE in str(error["exception"])
    (access,) = [r for r in records if r["event"] == "http.request"]
    assert access["status"] == 200
