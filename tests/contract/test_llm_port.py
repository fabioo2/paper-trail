"""LlmPort contract, for the scripted fake and the Ollama adapter (S3-AC-01, AC-A-01).

The graph suites trust the fake to behave like the adapter; this suite holds both to the same
observable behaviour. The adapter talks to a respx-mocked Ollama: no request leaves the process.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, StrictInt

from paper_trail.adapters.ollama.llm import OllamaLlm
from paper_trail.ports.llm import (
    ChatMessage,
    LlmOutputError,
    LlmPort,
    LlmRequest,
    LlmUnavailableError,
)
from tests.fakes.llm import ScriptedLlm
from tests.ollama_wire import BASE_URL, ollama_reply, ollama_settings

type Reply = dict[str, Any] | str


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cents: StrictInt


REQUEST = LlmRequest(
    model="text-model:test",
    tag="contract/v1",
    messages=(ChatMessage(role="system", content="Answer."), ChatMessage("user", "Total?")),
)


@dataclass
class Backend:
    """One LlmPort implementation, scripted with replies or with an unreachable host."""

    name: str
    router: respx.MockRouter | None

    def answering(self, *replies: Reply) -> LlmPort:
        if self.router is None:
            return ScriptedLlm(*replies)
        queue = [httpx.Response(200, json=ollama_reply(reply)) for reply in replies]
        self.router.post("/api/chat").mock(side_effect=queue)
        return OllamaLlm(ollama_settings())

    def unreachable(self, error: Exception) -> LlmPort:
        if self.router is None:
            return ScriptedLlm(LlmUnavailableError(str(error)))
        self.router.post("/api/chat").mock(side_effect=error)
        return OllamaLlm(ollama_settings())


@pytest.fixture(params=["scripted", "ollama"])
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    if request.param == "scripted":
        yield Backend("scripted", None)
        return
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield Backend("ollama", router)


def _ask(llm: LlmPort) -> Answer:
    return llm.complete_structured(REQUEST, Answer).value


def test_valid_output_is_returned_as_the_validated_model(backend: Backend) -> None:
    result = backend.answering({"total_cents": 2683}).complete_structured(REQUEST, Answer)

    assert result.value == Answer(total_cents=2683)
    assert isinstance(result.prompt_tokens, int)
    assert isinstance(result.completion_tokens, int)


@pytest.mark.parametrize(
    "bad",
    [{"total_cents": 26.83}, {"total_cents": 2683, "extra": 1}, "not JSON at all", {}],
    ids=["float-money", "extra-field", "not-json", "missing-field"],
)
def test_output_that_never_validates_is_an_output_error(backend: Backend, bad: Reply) -> None:
    """S3-AC-01 / S3-AC-11: invalid output, still invalid after the adapter's one repair."""
    with pytest.raises(LlmOutputError):
        _ask(backend.answering(bad, bad))


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["connection", "timeout"],
)
def test_an_unreachable_host_is_unavailable(backend: Backend, error: Exception) -> None:
    """S3-AC-01: connection errors and timeouts are LlmUnavailableError."""
    with pytest.raises(LlmUnavailableError):
        _ask(backend.unreachable(error))
