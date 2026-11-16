"""A canned Claude API for the ``AnthropicReader`` contract tests: the real SDK client over an
in-memory transport (``httpx2.MockTransport``), so no socket is opened.

``CannedApi(*replies)`` answers each request with the next reply: an ``httpx2.Response`` (built
with ``message()`` or ``error_response()``) or an exception the transport raises
(``httpx2.ConnectError``, ``httpx2.ReadTimeout``). ``then`` repeats once the replies are used
up (the SDK's retries); without it an extra request fails the test. Every request is kept, so
a test reads the body the SDK sent and its timeout.
"""

import json
from collections.abc import Mapping
from typing import Any

import httpx2
from anthropic import Anthropic

API_KEY = "sk-ant-test-0123456789abcdef"  # never a real key; tests assert it never leaks
INPUT_TOKENS = 1500
OUTPUT_TOKENS = 700

type Reply = httpx2.Response | Exception


def message(
    answer: Mapping[str, Any] | None = None,
    *,
    text: str | None = None,
    stop_reason: str = "end_turn",
    model: str = "claude-haiku-5-5",
) -> httpx2.Response:
    """A Messages API reply whose one text block is ``answer`` as JSON (or ``text``)."""
    content = (
        []
        if answer is None and text is None
        else [{"type": "text", "text": json.dumps(answer) if text is None else text}]
    )
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
        },
    )


def error_response(status: int, kind: str, detail: str) -> httpx2.Response:
    """An API error reply; ``retry-after-ms`` keeps the SDK's retries from waiting."""
    return httpx2.Response(
        status,
        headers={"retry-after-ms": "1"},
        json={"type": "error", "error": {"type": kind, "message": detail}},
    )


class CannedApi:
    def __init__(self, *replies: Reply, then: Reply | None = None) -> None:
        self._replies = list(replies)
        self._then = then
        self.requests: list[httpx2.Request] = []

    def client(self, **options: Any) -> Anthropic:
        """An SDK client on this transport; ``options`` are the adapter's own (timeout,
        retries) when a test lets the adapter build its client."""
        options.setdefault("api_key", API_KEY)
        return Anthropic(
            http_client=httpx2.Client(transport=httpx2.MockTransport(self._handle)), **options
        )

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._replies:
            reply = self._replies.pop(0)
        elif self._then is not None:
            reply = self._then
        else:
            msg = f"unexpected API request #{len(self.requests)}: no reply left"
            raise AssertionError(msg)
        if isinstance(reply, Exception):
            raise reply
        return reply
