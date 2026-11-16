"""A scripted stand-in for the Anthropic Messages API, listening on 127.0.0.1 only.

The CLI tests point ``ANTHROPIC_BASE_URL`` at it (the SDK's own variable, read when the client
is made; claude-extraction CQ17), so the real ``AnthropicReader`` and the whole composition
root run end to end while the "model" is a script: each ``POST /v1/messages`` answers with the
next reply. It never forwards anything anywhere; the Anthropic API is never contacted by a test.

A reply is the model's answer as JSON (a mapping: a text block, stop reason ``end_turn``) or a
``Canned`` failure: an error status (refusals and cut-off answers are owned by the adapter's
contract tests and the graph suites). Error statuses carry
``x-should-retry: false`` so the SDK does not retry them: one read is one request here, and the
SDK's own retries are pinned by the adapter's contract tests. With no reply left the fake
answers such a 500, which the reader reports as unavailable.
"""

import base64
import hashlib
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

INPUT_TOKENS = 1500
OUTPUT_TOKENS = 700


@dataclass(frozen=True, slots=True)
class Canned:
    """A reply that is no answer: an HTTP error ``status``."""

    status: int


UNAVAILABLE = Canned(status=529)

type Reply = Mapping[str, Any] | Canned


class FakeClaude:
    """The script and what was asked of it; served by ``serve_fake_claude``."""

    def __init__(self, *replies: Reply, then: Reply | None = None) -> None:
        self._replies = list(replies)
        self._then = then
        self.requests: list[dict[str, Any]] = []
        self.url = ""
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def models(self) -> list[str]:
        return [str(body.get("model")) for body in self.requests]

    def texts(self, call: int) -> list[str]:
        """The text blocks of the ``call``-th request's user turn (0-based), in order."""
        return [
            str(block["text"])
            for message in self.requests[call]["messages"]
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "text"
        ]

    def image_sha256(self, call: int) -> str:
        """SHA-256 of the image the ``call``-th request sent (its decoded base64 bytes)."""
        (data,) = [
            str(block["source"]["data"])
            for message in self.requests[call]["messages"]
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "image"
        ]
        return hashlib.sha256(base64.b64decode(data)).hexdigest()

    def answer(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self.requests.append(body)
            if self._replies:
                reply: Reply | None = self._replies.pop(0)
            else:
                reply = self._then
        if reply is None:
            return 500, _error("api_error", "fake claude: the script has no reply left")
        if isinstance(reply, Canned):
            return reply.status, _error("overloaded_error", "fake claude: overloaded")
        content = [{"type": "text", "text": json.dumps(reply)}]
        return 200, _message(body, content, "end_turn")


def _message(body: dict[str, Any], content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": body.get("model"),
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
    }


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


def _handler(fake: FakeClaude) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.split("?", 1)[0] != "/v1/messages":
                self._send(404, _error("not_found_error", f"unknown path {self.path}"))
                return
            self._send(*fake.answer(body))

        def do_GET(self) -> None:
            self._send(404, _error("not_found_error", f"unknown path {self.path}"))

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("x-should-retry", "false")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default stderr access log."""

    return Handler


@contextmanager
def serve_fake_claude(fake: FakeClaude) -> Iterator[FakeClaude]:
    """Serve ``fake`` on a free loopback port; ``fake.url`` is its base URL meanwhile."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
