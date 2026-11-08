"""The Ollama chat adapter against a respx-mocked host (S3-AC-01, AC-A-01, ADR-0009 amendment).

Pinned (docs/tests/agent-core-A.md):
- ``OllamaLlm(settings: OllamaSettings)``; requests go to ``POST <base_url>/api/chat`` with
  ``model``, ``messages`` (``role``, ``content``, and ``images`` as base64 strings only when the
  message has images), ``stream: false``, ``format`` = ``schema.model_json_schema()``,
  ``options.temperature`` from the request, ``options.num_ctx`` from settings,
  ``options.num_predict`` only when ``max_tokens`` is set, and ``keep_alive`` from settings;
  the httpx timeout is ``request_timeout_s``.
- One repair: when the content does not validate, a second request repeats the messages, adds
  the bad answer as an assistant message and a user message quoting the validation error; a
  second failure raises ``LlmOutputError``. Token counts are summed over both calls; a count
  the host did not report is None (when no call reported it).
- Connection errors, timeouts and non-2xx answers raise ``LlmUnavailableError`` whose message
  names the host (``llm-host.test:11434``); with no ``base_url`` it says "not configured" and
  sends nothing.
"""

import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, StrictInt

from paper_trail.adapters.ollama.llm import OllamaLlm
from paper_trail.config import OllamaSettings
from paper_trail.ports.llm import (
    ChatMessage,
    LlmOutputError,
    LlmRequest,
    LlmUnavailableError,
)
from tests.ollama_wire import BASE_URL, ollama_reply, ollama_settings


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cents: StrictInt


IMAGE = b"\xff\xd8\xff\xe0fake jpeg"
REQUEST = LlmRequest(
    model="qwen2.5:7b-instruct",
    tag="extraction/v1",
    messages=(
        ChatMessage(role="system", content="You read receipts."),
        ChatMessage(role="user", content="<receipt_data>\nTOTAL 26.83\n</receipt_data>"),
    ),
)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


def _bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


def test_s3_ac_01_request_body(router: respx.MockRouter) -> None:
    """S3-AC-01: model, temperature 0, num_ctx from settings, schema as format, keep_alive."""
    route = router.post("/api/chat").mock(
        return_value=httpx.Response(200, json=ollama_reply({"total_cents": 2683}))
    )
    llm = OllamaLlm(ollama_settings(num_ctx=8192, keep_alive="7m"))

    result = llm.complete_structured(REQUEST, Answer)

    (body,) = _bodies(route)
    assert body["model"] == "qwen2.5:7b-instruct"
    assert body["stream"] is False
    assert body["format"] == Answer.model_json_schema()
    assert body["options"]["temperature"] == 0.0
    assert body["options"]["num_ctx"] == 8192
    assert "num_predict" not in body["options"]
    assert body["keep_alive"] == "7m"
    assert body["messages"] == [
        {"role": "system", "content": "You read receipts."},
        {"role": "user", "content": "<receipt_data>\nTOTAL 26.83\n</receipt_data>"},
    ]
    assert result.value == Answer(total_cents=2683)
    assert (result.prompt_tokens, result.completion_tokens) == (11, 7)


def test_s3_ac_01_images_are_sent_as_base64(router: respx.MockRouter) -> None:
    """S3-AC-01: images for the vision model as base64 strings on their message."""
    route = router.post("/api/chat").mock(
        return_value=httpx.Response(200, json=ollama_reply({"total_cents": 1}))
    )
    request = LlmRequest(
        model="qwen2.5vl:7b",
        tag="extraction/v1",
        messages=(ChatMessage(role="user", content="Read this.", images=(IMAGE,)),),
        max_tokens=512,
    )

    OllamaLlm(ollama_settings()).complete_structured(request, Answer)

    (body,) = _bodies(route)
    assert body["messages"][0]["images"] == [base64.b64encode(IMAGE).decode("ascii")]
    assert body["options"]["num_predict"] == 512


def test_s3_ac_01_one_repair_request_quotes_the_validation_error(
    router: respx.MockRouter,
) -> None:
    """S3-AC-01: exactly one repair request including the validation error."""
    route = router.post("/api/chat").mock(
        side_effect=[
            httpx.Response(200, json=ollama_reply({"total_cents": "26.83"})),
            httpx.Response(200, json=ollama_reply({"total_cents": 2683})),
        ]
    )

    result = OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)

    first, repair = _bodies(route)
    assert repair["messages"][: len(first["messages"])] == first["messages"]
    assistant, user = repair["messages"][len(first["messages"]) :]
    assert assistant == {"role": "assistant", "content": json.dumps({"total_cents": "26.83"})}
    assert user["role"] == "user"
    assert "total_cents" in user["content"]
    assert repair["format"] == first["format"]
    assert result.value == Answer(total_cents=2683)
    assert (result.prompt_tokens, result.completion_tokens) == (22, 14)


def test_s3_ac_01_a_second_invalid_answer_is_an_output_error(router: respx.MockRouter) -> None:
    route = router.post("/api/chat").mock(
        return_value=httpx.Response(200, json=ollama_reply("no json here"))
    )

    with pytest.raises(LlmOutputError):
        OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)

    assert route.call_count == 2


def test_missing_token_counts_are_none(router: respx.MockRouter) -> None:
    router.post("/api/chat").mock(
        return_value=httpx.Response(
            200,
            json=ollama_reply({"total_cents": 1}, prompt_tokens=None, completion_tokens=None),
        )
    )

    result = OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)

    assert (result.prompt_tokens, result.completion_tokens) == (None, None)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("connect timeout"),
        httpx.ReadTimeout("read timeout"),
        httpx.Response(500, json={"error": "model failed to load"}),
        httpx.Response(404, json={"error": "model 'qwen2.5:7b-instruct' not found"}),
    ],
    ids=["refused", "connect-timeout", "read-timeout", "server-error", "model-missing"],
)
def test_ac_a_01_unavailable_errors_name_the_host(
    router: respx.MockRouter, answer: Exception | httpx.Response
) -> None:
    """AC-A-01: LlmUnavailableError's message names the host, so the CLI can say which."""
    router.post("/api/chat").mock(side_effect=[answer])

    with pytest.raises(LlmUnavailableError, match=r"llm-host\.test:11434"):
        OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)


def test_ac_a_01_base_url_comes_from_settings() -> None:
    """AC-A-01: the host comes from settings; a path prefix and trailing slash are kept."""
    settings = OllamaSettings.model_validate({"base_url": "http://other-host.test:8080/ollama/"})
    with respx.mock(assert_all_called=False) as other:
        route = other.post("http://other-host.test:8080/ollama/api/chat").mock(
            return_value=httpx.Response(200, json=ollama_reply({"total_cents": 5}))
        )

        answer = OllamaLlm(settings).complete_structured(REQUEST, Answer)

    assert answer.value.total_cents == 5
    assert route.call_count == 1


def test_ac_a_01_not_configured_sends_nothing(router: respx.MockRouter) -> None:
    route = router.post("/api/chat")

    with pytest.raises(LlmUnavailableError, match="not configured"):
        OllamaLlm(OllamaSettings()).complete_structured(REQUEST, Answer)

    assert route.call_count == 0


def test_the_request_timeout_comes_from_settings(router: respx.MockRouter) -> None:
    route = router.post("/api/chat").mock(
        return_value=httpx.Response(200, json=ollama_reply({"total_cents": 1}))
    )

    OllamaLlm(ollama_settings(request_timeout_s=42.0)).complete_structured(REQUEST, Answer)

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] == 42.0


# --- bounded output (real run 2026-11-08: a sideways photo looped to num_predict, then the repair
# round re-sent the whole answer plus the image and overflowed the context) ----------------------

CONTEXT_OVERFLOW = "request (12383 tokens) exceeds the available context size (12288 tokens)"


def test_a_truncated_answer_is_an_output_error_without_a_repair(
    router: respx.MockRouter,
) -> None:
    cut = ollama_reply('{"total_cents": 26') | {"done_reason": "length"}
    route = router.post("/api/chat").mock(return_value=httpx.Response(200, json=cut))

    with pytest.raises(LlmOutputError, match="cut off"):
        OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)

    assert route.call_count == 1


def test_a_valid_answer_that_stopped_normally_needs_no_repair(router: respx.MockRouter) -> None:
    done = ollama_reply({"total_cents": 2683}) | {"done_reason": "stop"}
    route = router.post("/api/chat").mock(return_value=httpx.Response(200, json=done))

    answer = OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)

    assert answer.value.total_cents == 2683
    assert route.call_count == 1


@pytest.mark.parametrize("reason", [CONTEXT_OVERFLOW, "input length exceeds the context length"])
def test_a_context_overflow_is_an_output_error_not_an_outage(
    router: respx.MockRouter, reason: str
) -> None:
    router.post("/api/chat").mock(return_value=httpx.Response(400, json={"error": reason}))

    with pytest.raises(LlmOutputError, match="context"):
        OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)


def test_a_context_overflow_on_the_repair_round_is_an_output_error(
    router: respx.MockRouter,
) -> None:
    router.post("/api/chat").mock(
        side_effect=[
            httpx.Response(200, json=ollama_reply({"total": "26.83"})),
            httpx.Response(400, json={"error": CONTEXT_OVERFLOW}),
        ]
    )

    with pytest.raises(LlmOutputError):
        OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)


def test_another_bad_request_is_still_unavailable(router: respx.MockRouter) -> None:
    router.post("/api/chat").mock(
        return_value=httpx.Response(400, json={"error": "invalid format"})
    )

    with pytest.raises(LlmUnavailableError, match=r"llm-host\.test:11434"):
        OllamaLlm(ollama_settings()).complete_structured(REQUEST, Answer)
