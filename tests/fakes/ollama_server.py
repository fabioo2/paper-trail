"""A scripted stand-in for Ollama's native HTTP API, listening on 127.0.0.1 only.

The CLI tests point ``PAPER_TRAIL_OLLAMA__BASE_URL`` at it, so the real ``OllamaLlm`` adapter
and the whole composition root run end to end while the "model" is a script: each
``POST /api/chat`` answers with the next reply as the assistant message's content, and each
``POST /api/embed`` with one deterministic 768-dimension vector per input text (the vectors of
``tests.fakes.embeddings``: a table entry, else a hashed vector). It never forwards anything
anywhere; the real LLM host is never contacted by a test.
"""

import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.fakes.embeddings import FakeEmbeddings

PROMPT_TOKENS = 120
COMPLETION_TOKENS = 80


class FakeOllama:
    """The script and what was asked of it; served by ``serve_fake_ollama``."""

    def __init__(
        self,
        *replies: Mapping[str, Any] | str,
        then: Mapping[str, Any] | None = None,
        embeddings: Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        self._replies = list(replies)
        self._then = then
        self._embeddings = FakeEmbeddings(embeddings)
        self.chat_requests: list[dict[str, Any]] = []
        self.embed_requests: list[dict[str, Any]] = []
        self.url = ""
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        return len(self.chat_requests)

    def answer(self, body: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self.chat_requests.append(body)
            if self._replies:
                reply: Mapping[str, Any] | str = self._replies.pop(0)
            elif self._then is not None:
                reply = self._then
            else:
                return None
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return {
            "model": body.get("model"),
            "message": {"role": "assistant", "content": content},
            "done": True,
            "prompt_eval_count": PROMPT_TOKENS,
            "eval_count": COMPLETION_TOKENS,
        }

    def embed(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.embed_requests.append(body)
        texts = body.get("input", [])
        texts = [texts] if isinstance(texts, str) else list(texts)
        return {
            "model": body.get("model"),
            "embeddings": [list(self._embeddings.vector(str(text))) for text in texts],
        }


def _handler(fake: FakeOllama) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/embed":
                self._send(200, fake.embed(body))
                return
            if self.path != "/api/chat":
                self._send(404, {"error": f"unknown path {self.path}"})
                return
            answer = fake.answer(body)
            if answer is None:
                self._send(500, {"error": "fake ollama: the script has no reply left"})
                return
            self._send(200, answer)

        def do_GET(self) -> None:
            self._send(404, {"error": f"unknown path {self.path}"})

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            """Silence the default stderr access log."""

    return Handler


@contextmanager
def serve_fake_ollama(fake: FakeOllama) -> Iterator[FakeOllama]:
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


def closed_loopback_url() -> str:
    """A loopback URL on which nothing listens (connection refused): an unreachable LLM host."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"
