"""Talking to Ollama over HTTP, shared by the chat and embedding adapters (ADR-0009).

Every failure to get a JSON answer (not configured, refused, timed out, an error status, not
JSON) is an ``LlmUnavailableError`` naming the host, after any retries the caller asked for, so
a run stops with its checkpoint and can resume when the host is back. One exception: a request
longer than the model's context is the request's fault, not the host's, so it is an
``LlmOutputError`` (a failed attempt).
"""

from collections.abc import Callable
from typing import Any

import httpx

from paper_trail.config import OllamaSettings
from paper_trail.ports.llm import LlmOutputError, LlmUnavailableError

# Ollama's 400 for a request that does not fit the loaded context (num_ctx) names the context
# ("exceeds the available context size", "input length exceeds the context length").
_CONTEXT = "context"


def ollama_client(settings: OllamaSettings) -> httpx.Client:
    """A client for the configured host, with the request timeout."""
    base_url = settings.base_url
    if base_url is None:
        msg = "the LLM host is not configured (set PAPER_TRAIL_OLLAMA__BASE_URL)"
        raise LlmUnavailableError(msg)
    return httpx.Client(
        base_url=str(base_url).rstrip("/") + "/", timeout=settings.request_timeout_s
    )


def post_json(
    client: httpx.Client,
    path: str,
    body: dict[str, Any],
    *,
    read_timeout_retries: int = 0,
    on_retry: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """POST ``body`` to ``path`` and return the JSON object answered.

    A request that times out waiting for the answer is sent again, unchanged and at once, up to
    ``read_timeout_retries`` more times; ``on_retry`` gets the number of the try that timed out
    before each one. Any answer, an error status included, ends the tries.
    """
    host = _host(client)
    tries = 0
    while True:
        tries += 1
        try:
            response = client.post(path, json=body)
            break
        except httpx.ReadTimeout:
            if tries > read_timeout_retries:
                msg = f"the LLM host {host} timed out {tries} time{'s' if tries > 1 else ''}"
                raise LlmUnavailableError(msg) from None
            if on_retry is not None:
                on_retry(tries)
        except httpx.TimeoutException:
            msg = f"the LLM host {host} timed out"
            raise LlmUnavailableError(msg) from None
        except httpx.HTTPError as error:
            msg = f"cannot reach the LLM host {host}: {type(error).__name__}"
            raise LlmUnavailableError(msg) from None
    if response.is_error:
        reason = _error_text(response)
        msg = f"the LLM host {host} answered {response.status_code}: {reason}"
        if response.status_code == httpx.codes.BAD_REQUEST and _CONTEXT in reason.lower():
            raise LlmOutputError(msg)
        raise LlmUnavailableError(msg)
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        msg = f"the LLM host {host} did not answer with a JSON object"
        raise LlmUnavailableError(msg)
    return answer


def _host(client: httpx.Client) -> str:
    url = client.base_url
    return f"{url.host}:{url.port}" if url.port is not None else url.host


def _error_text(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", response.reason_phrase))
    except (ValueError, AttributeError):
        return response.reason_phrase
