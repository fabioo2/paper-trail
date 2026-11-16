"""A scripted Claude (implements ``paper_trail.ports.reader.ReceiptReader``): canned replies,
one per read.

Every graph test drives extraction through this fake, never the Anthropic API. A reply is the
model's answer as JSON (a mapping, validated against the requested schema the way the adapter
validates ``parsed_output``: a mismatch raises ``ReaderOutputError`` with the validation text),
or an exception instance, which is raised (``ReaderUnavailableError`` for an API that is down,
``ReaderRefusedError``, ``ReaderTruncatedError``).

``then`` is repeated once the scripted replies are used up ("always inconsistent"); without it
an extra read fails the test, so a test also pins how many reads a run makes. Every request is
recorded: its model, its text (a retry's carries the feedback) and the image bytes.
"""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from paper_trail.ports.reader import (
    ReaderOutputError,
    ReadRequest,
    ReadResult,
    ReadUsage,
)

type Reply = Mapping[str, Any] | BaseException

INPUT_TOKENS = 1500
OUTPUT_TOKENS = 700
USAGE = ReadUsage(input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS)
END_TURN = "end_turn"


class ScriptedReader:
    def __init__(self, *replies: Reply, then: Reply | None = None) -> None:
        self._replies = list(replies)
        self._then = then
        self.requests: list[ReadRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def models(self) -> list[str]:
        return [request.model for request in self.requests]

    def script(self, *replies: Reply) -> None:
        """Queue more replies (a second process on the same fake)."""
        self._replies.extend(replies)

    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        self.requests.append(request)
        reply = self._next_reply()
        if isinstance(reply, BaseException):
            raise reply
        try:
            value = TypeAdapter(schema).validate_json(json.dumps(reply))
        except ValidationError as error:
            error_text = str(error)
            raise ReaderOutputError(error_text) from error
        return ReadResult(value=value, usage=USAGE, stop_reason=END_TURN)

    def _next_reply(self) -> Reply:
        if self._replies:
            return self._replies.pop(0)
        if self._then is not None:
            return self._then
        msg = f"unexpected Claude read #{self.calls}: the script has no reply left"
        raise AssertionError(msg)
