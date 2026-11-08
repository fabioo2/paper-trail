"""A scripted LLM (implements ``paper_trail.ports.llm.LlmPort``): canned replies, one per call.

Every graph test drives the model through this fake, never a real Ollama. A reply is a JSON
object (a mapping, validated against the requested schema exactly like the Ollama adapter
validates the model's text), a raw string (for output that is not even JSON), or an exception
instance, which is raised (e.g. ``LlmUnavailableError`` for a GPU that dropped). Output that
fails validation raises ``LlmOutputError``, as the adapter does after its one repair attempt.

``then`` is repeated once the scripted replies are used up ("always inconsistent"); without it
an extra call fails the test, so a test also pins how many calls a run makes.
"""

import json
from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from paper_trail.ports.llm import LlmOutputError, LlmRequest, LlmResult

type Reply = Mapping[str, Any] | str | BaseException

PROMPT_TOKENS = 120
COMPLETION_TOKENS = 80


class ScriptedLlm:
    def __init__(self, *replies: Reply, then: Reply | None = None) -> None:
        self._replies = list(replies)
        self._then = then
        self.requests: list[LlmRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete_structured[M](self, request: LlmRequest, schema: type[M]) -> LlmResult[M]:
        self.requests.append(request)
        reply = self._next_reply()
        if isinstance(reply, BaseException):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        try:
            value = TypeAdapter(schema).validate_json(text)
        except ValidationError as error:
            msg = f"the model's output does not match {schema.__name__}: {error}"
            raise LlmOutputError(msg) from error
        return LlmResult(
            value=value, prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS
        )

    def contents(self, call: int) -> list[str]:
        """The message contents of the ``call``-th request (0-based), in order."""
        return [message.content for message in self.requests[call].messages]

    def last_user_message(self, call: int) -> str:
        """The final user message of the ``call``-th request: the receipt to extract."""
        user = [
            message.content for message in self.requests[call].messages if message.role == "user"
        ]
        return user[-1]

    def _next_reply(self) -> Reply:
        if self._replies:
            return self._replies.pop(0)
        if self._then is not None:
            return self._then
        msg = f"unexpected LLM call #{self.calls}: the script has no reply left"
        raise AssertionError(msg)
