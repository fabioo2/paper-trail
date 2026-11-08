"""``LlmPort`` on Ollama's native chat API (ADR-0009 and its amendment, S3-AC-01).

``POST <base_url>/api/chat`` with the answer's JSON schema as ``format``, temperature from the
request and one ``num_ctx`` from settings (changing it between requests reloads the model). An
answer that does not validate gets exactly one repair request that quotes the validation
error; a second bad answer is an ``LlmOutputError``. An answer cut off at ``num_predict``
(``done_reason`` "length") that does not validate is an ``LlmOutputError`` at once: a repair
would re-send the whole long answer and overflow the context. Connection failures, timeouts and
error statuses are ``LlmUnavailableError`` naming the host (a context overflow excepted, see
``http``).
"""

import base64
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from paper_trail.adapters.ollama.http import ollama_client, post_json
from paper_trail.config import OllamaSettings
from paper_trail.ports.llm import ChatMessage, LlmOutputError, LlmRequest, LlmResult

_REPAIR = (
    "Your answer did not match the JSON schema. Answer again with one JSON object that "
    "matches it exactly. Problems: {problems}"
)


class OllamaLlm:
    """Implements ``paper_trail.ports.llm.LlmPort``."""

    def __init__(self, settings: OllamaSettings) -> None:
        self._settings = settings

    def complete_structured[M](self, request: LlmRequest, schema: type[M]) -> LlmResult[M]:
        adapter = TypeAdapter(schema)
        messages = [_message(message) for message in request.messages]
        body = self._body(request, adapter.json_schema(), messages)
        with ollama_client(self._settings) as client:
            content, prompt_tokens, completion_tokens, truncated = self._chat(client, body)
            try:
                value = adapter.validate_json(content)
            except ValidationError as error:
                if truncated:
                    msg = f"the answer was cut off at {completion_tokens} tokens"
                    raise LlmOutputError(msg) from None
                problems = _problems(error)
                body["messages"] = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": _REPAIR.format(problems=problems)},
                ]
                content, repair_prompt, repair_completion, _ = self._chat(client, body)
                prompt_tokens = _sum(prompt_tokens, repair_prompt)
                completion_tokens = _sum(completion_tokens, repair_completion)
                try:
                    value = adapter.validate_json(content)
                except ValidationError as again:
                    msg = f"the answer does not match {_name(schema)}: {_problems(again)}"
                    raise LlmOutputError(msg) from None
        return LlmResult(
            value=value, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )

    def _body(
        self, request: LlmRequest, schema: dict[str, Any], messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": request.temperature,
            "num_ctx": self._settings.num_ctx,
        }
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        return {
            "model": request.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": options,
            "keep_alive": self._settings.keep_alive,
        }

    @staticmethod
    def _chat(
        client: httpx.Client, body: dict[str, Any]
    ) -> tuple[str, int | None, int | None, bool]:
        """The answer's content, token counts and whether it was cut off at ``num_predict``."""
        answer = post_json(client, "api/chat", body)
        message = answer.get("message")
        content = message.get("content", "") if isinstance(message, dict) else ""
        truncated = answer.get("done_reason") == "length"
        return str(content), answer.get("prompt_eval_count"), answer.get("eval_count"), truncated


def _message(message: ChatMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.images:
        wire["images"] = [base64.b64encode(image).decode("ascii") for image in message.images]
    return wire


def _problems(error: ValidationError) -> str:
    """Where and why the answer is wrong, without quoting the answer back."""
    return "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or '(answer)'}: {detail['msg']}"
        for detail in error.errors()
    )


def _sum(first: int | None, second: int | None) -> int | None:
    if first is None and second is None:
        return None
    return (first or 0) + (second or 0)


def _name(schema: type[Any]) -> str:
    return getattr(schema, "__name__", repr(schema))
