"""A chat request that times out reading the answer is sent again (reliability plan R1-R5).

Pinned (plan.md R1-R5, A4, answers Q1-Q3, Q5, Q8, Q9):
- Only ``httpx.ReadTimeout`` is retried, immediately and with the same body, up to
  ``OllamaSettings.timeout_retries`` more times (default 1, 0..3). Connect, write and pool
  timeouts, other transport errors, error statuses, a non-JSON answer and the context overflow
  behave as before: one request.
- When every try timed out: ``LlmUnavailableError`` naming the host and "timed out N time(s)", N
  the tries of the request that failed (the repair request counts its own).
- The schema repair is one separate request with its own retries. ``LlmResult.timeout_retries``,
  or ``timeout_retries`` on the error when the call fails, sums the retried read timeouts of the
  call; a failure's message is the same as without retries (it reaches the model's next prompt).
- One structlog warning ``llm.timeout_retry`` per retried try: model, try_number (1-based,
  restarting for the repair), timeout_s and repair.
- Embedding requests are never retried.
"""

import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, StrictInt
from structlog.testing import capture_logs

from paper_trail.adapters.ollama.embeddings import OllamaEmbeddings
from paper_trail.adapters.ollama.llm import OllamaLlm
from paper_trail.ports.llm import (
    ChatMessage,
    LlmError,
    LlmOutputError,
    LlmRequest,
    LlmResult,
    LlmUnavailableError,
)
from tests.ollama_wire import BASE_URL, ollama_reply, ollama_settings


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cents: StrictInt


TEXT_MODEL = "qwen2.5:7b-instruct"
IMAGE = b"\xff\xd8\xff\xe0fake jpeg"
TEXT_REQUEST = LlmRequest(
    model=TEXT_MODEL,
    tag="extraction/v1",
    messages=(
        ChatMessage(role="system", content="You read receipts."),
        ChatMessage(role="user", content="<receipt_data>\nTOTAL 26.83\n</receipt_data>"),
    ),
)
VISION_MODEL = "vision-model:test"  # not a settings default, so a logged model comes from here
VISION_REQUEST = LlmRequest(
    model=VISION_MODEL,
    tag="vision/v1",
    messages=(ChatMessage(role="user", content="Read this.", images=(IMAGE,)),),
    max_tokens=512,
)
HOST = r"llm-host\.test:11434"
THE_HOST = "the LLM host llm-host.test:11434"
CONTEXT_OVERFLOW = "request (12383 tokens) exceeds the available context size (12288 tokens)"
RETRY_EVENT = "llm.timeout_retry"


def timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("read timeout")


def ok() -> httpx.Response:
    return httpx.Response(200, json=ollama_reply({"total_cents": 2683}))


def bad() -> httpx.Response:
    return httpx.Response(200, json=ollama_reply({"total_cents": "26.83"}))


def overflow() -> httpx.Response:
    return httpx.Response(400, json={"error": CONTEXT_OVERFLOW})


def failed() -> httpx.Response:
    return httpx.Response(500, json={"error": "model failed to load"})


def answered(retries: int, prompt_tokens: int = 11, completion_tokens: int = 7) -> LlmResult[Any]:
    return LlmResult(
        value=Answer(total_cents=2683),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        timeout_retries=retries,
    )


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


type Reply = Callable[[], Exception | httpx.Response]
type Script = tuple[Reply, ...]


def _chat(router: respx.MockRouter, *script: Reply) -> respx.Route:
    """The host answers each request with the next of ``script``, built fresh per test."""
    return router.post("/api/chat").mock(side_effect=[answer() for answer in script])


def _bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


def _shapes(bodies: Sequence[dict[str, Any]]) -> list[str]:
    """Each body as "first" (equal to the first request) or "repair" (the first request's
    messages plus the bad answer and the repair prompt); anything else fails the test."""
    first = bodies[0]
    shapes = []
    for body in bodies:
        if body == first:
            shapes.append("first")
            continue
        assert body == {**first, "messages": body["messages"]}
        assert body["messages"][: len(first["messages"])] == first["messages"]
        assert len(body["messages"]) == len(first["messages"]) + 2
        shapes.append("repair")
    repairs = [body for body, shape in zip(bodies, shapes, strict=True) if shape == "repair"]
    assert all(body == repairs[0] for body in repairs)
    return shapes


def _no_sleep(seconds: float) -> None:
    msg = f"the retry waited {seconds} s"
    raise AssertionError(msg)


def _llm(retries: int, **overrides: Any) -> OllamaLlm:
    return OllamaLlm(ollama_settings(timeout_retries=retries, **overrides))


# --- RL-TC-04: a read timeout is retried with the same body ------------------------------------


@pytest.mark.parametrize("request_", [TEXT_REQUEST, VISION_REQUEST], ids=["text", "vision"])
def test_rl_tc_04_a_read_timeout_is_sent_again_unchanged(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, request_: LlmRequest
) -> None:
    """At once: nothing waits between the tries."""
    monkeypatch.setattr(time, "sleep", _no_sleep)
    route = _chat(router, timeout, ok)

    result = _llm(1, request_timeout_s=42.0).complete_structured(request_, Answer)

    assert result == answered(retries=1)
    first, second = _bodies(route)
    assert second == first
    assert [call.request.extensions["timeout"]["read"] for call in route.calls] == [42.0, 42.0]


# --- RL-TC-05: every try times out ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("retries", "message"),
    [
        (0, f"{THE_HOST} timed out 1 time"),
        (1, f"{THE_HOST} timed out 2 times"),
        (2, f"{THE_HOST} timed out 3 times"),
        (3, f"{THE_HOST} timed out 4 times"),
    ],
)
def test_rl_tc_05_every_try_timed_out_is_unavailable_and_counts_the_tries(
    router: respx.MockRouter, retries: int, message: str
) -> None:
    route = _chat(router, *[timeout] * (retries + 1))

    with pytest.raises(LlmUnavailableError) as raised:
        _llm(retries).complete_structured(TEXT_REQUEST, Answer)

    assert (route.call_count, str(raised.value), raised.value.timeout_retries) == (
        retries + 1,
        message,
        retries,
    )


# --- RL-TC-06 (Q1, Q9): only a read timeout is retried -------------------------------------------


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.ConnectError("connection refused"), LlmUnavailableError),
        (httpx.ConnectTimeout("connect timeout"), LlmUnavailableError),
        (httpx.WriteTimeout("write timeout"), LlmUnavailableError),
        (httpx.PoolTimeout("pool timeout"), LlmUnavailableError),
        (httpx.ReadError("connection reset"), LlmUnavailableError),
        (httpx.Response(500, json={"error": "model failed to load"}), LlmUnavailableError),
        (httpx.Response(404, json={"error": "model not found"}), LlmUnavailableError),
        (httpx.Response(200, text="<html>proxy error</html>"), LlmUnavailableError),
        (overflow(), LlmOutputError),
    ],
    ids=[
        "refused",
        "connect-timeout",
        "write-timeout",
        "pool-timeout",
        "read-error",
        "server-error",
        "model-missing",
        "not-json",
        "context-overflow",
    ],
)
def test_rl_tc_06_anything_but_a_read_timeout_is_sent_once(
    router: respx.MockRouter, answer: Exception | httpx.Response, error: type[LlmError]
) -> None:
    """Also AC-A-01: an unavailable host is named in the message, so the CLI can say which."""
    route = _chat(router, lambda: answer, ok, ok)

    with pytest.raises(error) as raised:
        _llm(2).complete_structured(TEXT_REQUEST, Answer)

    assert (type(raised.value), route.call_count, raised.value.timeout_retries) == (error, 1, 0)
    if error is LlmUnavailableError:
        assert raised.match(HOST)


# --- RL-TC-07: an answer ends the retrying; retry and repair are separate budgets --------------


# (name, retries, script, (timeout_retries, prompt tokens, completion tokens), request shapes)
FINISHED_RUNS: list[tuple[str, int, Script, tuple[int, int, int], list[str]]] = [
    ("retry then repair", 1, (timeout, bad, ok), (1, 22, 14), ["first"] * 2 + ["repair"]),
    ("repair retried", 1, (bad, timeout, ok), (1, 22, 14), ["first"] + ["repair"] * 2),
    (
        "both budgets used",
        1,
        (timeout, bad, timeout, ok),
        (2, 22, 14),
        ["first", "first", "repair", "repair"],
    ),
]

SERVER_ERROR = f"{THE_HOST} answered 500: model failed to load"
NO_MATCH = "the answer does not match Answer: total_cents: Input should be a valid integer"

# (name, retries, script, (error, its exact message, its timeout_retries), request shapes).
# The messages are those of the same failure without a retry.
FAILED_RUNS: list[tuple[str, int, Script, tuple[type[Exception], str, int], list[str]]] = [
    (
        "error answer after a retry",
        1,
        (timeout, failed),
        (LlmUnavailableError, SERVER_ERROR, 1),
        ["first"] * 2,
    ),
    (
        "error answer, retries left",
        2,
        (timeout, failed, ok),
        (LlmUnavailableError, SERVER_ERROR, 1),
        ["first"] * 2,
    ),
    (
        "error answer after two retries",
        2,
        (timeout, timeout, failed),
        (LlmUnavailableError, SERVER_ERROR, 2),
        ["first"] * 3,
    ),
    (
        "repair times out every try",
        1,
        (bad, timeout, timeout),
        (LlmUnavailableError, f"{THE_HOST} timed out 2 times", 1),
        ["first", "repair", "repair"],
    ),
    (
        "repair times out every try after a retry",
        1,
        (timeout, bad, timeout, timeout),
        (LlmUnavailableError, f"{THE_HOST} timed out 2 times", 2),
        ["first", "first", "repair", "repair"],
    ),
    (
        "repair fails after a retry",
        1,
        (timeout, bad, failed),
        (LlmUnavailableError, SERVER_ERROR, 1),
        ["first", "first", "repair"],
    ),
    (
        "still one repair",
        1,
        (timeout, bad, bad),
        (LlmOutputError, NO_MATCH, 1),
        ["first", "first", "repair"],
    ),
    (
        "overflow after a retry",
        1,
        (timeout, overflow),
        (LlmOutputError, f"{THE_HOST} answered 400: {CONTEXT_OVERFLOW}", 1),
        ["first"] * 2,
    ),
]


@pytest.mark.parametrize(
    ("retries", "script", "expected", "shapes"),
    [row[1:] for row in FINISHED_RUNS],
    ids=[row[0] for row in FINISHED_RUNS],
)
def test_rl_tc_07_retries_and_the_repair_finish_the_call(
    router: respx.MockRouter,
    retries: int,
    script: Script,
    expected: tuple[int, int, int],
    shapes: list[str],
) -> None:
    route = _chat(router, *script)

    result = _llm(retries).complete_structured(TEXT_REQUEST, Answer)

    assert (result, _shapes(_bodies(route))) == (answered(*expected), shapes)


@pytest.mark.parametrize(
    ("retries", "script", "expected", "shapes"),
    [row[1:] for row in FAILED_RUNS],
    ids=[row[0] for row in FAILED_RUNS],
)
def test_rl_tc_07_an_answer_or_spent_budget_ends_the_call(
    router: respx.MockRouter,
    retries: int,
    script: Script,
    expected: tuple[type[Exception], str, int],
    shapes: list[str],
) -> None:
    route = _chat(router, *script)

    with pytest.raises(LlmError) as raised:
        _llm(retries).complete_structured(TEXT_REQUEST, Answer)

    error = raised.value
    assert (type(error), str(error), error.timeout_retries) == expected
    assert _shapes(_bodies(route)) == shapes


# --- RL-TC-08 (Q3): one warning per retried try --------------------------------------------------


def _warning(try_number: int, *, repair: bool = False) -> dict[str, Any]:
    return {
        "event": RETRY_EVENT,
        "log_level": "warning",
        "model": VISION_MODEL,
        "try_number": try_number,
        "timeout_s": 42.0,
        "repair": repair,
    }


@pytest.mark.parametrize(
    ("retries", "script", "expected"),
    [
        (2, (timeout, timeout, ok), [_warning(1), _warning(2)]),
        (1, (timeout, timeout), [_warning(1)]),
        (0, (timeout,), []),
        (1, (ok,), []),
        (1, (bad, timeout, ok), [_warning(1, repair=True)]),
        (1, (timeout, bad, timeout, ok), [_warning(1), _warning(1, repair=True)]),
    ],
    ids=[
        "two retries",
        "the last try is not retried",
        "no retries",
        "no timeout",
        "repair numbered from 1",
        "both requests",
    ],
)
def test_rl_tc_08_each_retried_try_logs_one_warning(
    router: respx.MockRouter, retries: int, script: Script, expected: list[dict[str, Any]]
) -> None:
    _chat(router, *script)

    with capture_logs() as logs, suppress(LlmUnavailableError):  # the outcome is RL-TC-05's
        _llm(retries, request_timeout_s=42.0).complete_structured(VISION_REQUEST, Answer)

    assert [log for log in logs if log.get("event") == RETRY_EVENT] == expected


# --- RL-TC-09: embeddings are never retried -------------------------------------------------------


def test_rl_tc_09_an_embedding_read_timeout_is_not_retried(router: respx.MockRouter) -> None:
    route = router.post("/api/embed").mock(side_effect=[timeout(), timeout(), timeout()])

    with capture_logs() as logs, pytest.raises(LlmUnavailableError, match=HOST):
        OllamaEmbeddings(ollama_settings(timeout_retries=2)).embed(["eggs"])

    assert route.call_count == 1
    assert [log for log in logs if log.get("event") == RETRY_EVENT] == []
