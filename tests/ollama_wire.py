"""Ollama's native chat wire format, shared by the respx-mocked adapter suites.

``BASE_URL`` is a reserved ``.test`` name: nothing resolves it, and respx answers before any
connection would be made. No test talks to a real Ollama.
"""

import json
from typing import Any

from paper_trail.config import OllamaSettings

BASE_URL = "http://llm-host.test:11434"


def ollama_reply(
    content: dict[str, Any] | str,
    *,
    prompt_tokens: int | None = 11,
    completion_tokens: int | None = 7,
) -> dict[str, Any]:
    """The body of a non-streaming ``POST /api/chat`` answer (token counts omitted when None)."""
    body: dict[str, Any] = {
        "model": "text-model:test",
        "message": {
            "role": "assistant",
            "content": content if isinstance(content, str) else json.dumps(content),
        },
        "done": True,
    }
    if prompt_tokens is not None:
        body["prompt_eval_count"] = prompt_tokens
    if completion_tokens is not None:
        body["eval_count"] = completion_tokens
    return body


def ollama_settings(**overrides: Any) -> OllamaSettings:
    return OllamaSettings.model_validate({"base_url": BASE_URL, **overrides})
