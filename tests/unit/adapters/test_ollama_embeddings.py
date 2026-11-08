"""The Ollama embedding adapter against a respx-mocked host (S4-AC-08, ADR-0009 amendment).

Pinned (docs/tests/agent-core-C.md):
- ``OllamaEmbeddings(settings: OllamaSettings)`` in ``paper_trail.adapters.ollama.embeddings``
  implements ``EmbeddingPort``; one ``POST <base_url>/api/embed`` per ``embed`` call with
  ``{"model": settings.embed_model, "input": [texts...], "keep_alive": settings.keep_alive}``
  and the httpx timeout ``request_timeout_s``; the answer's ``embeddings`` come back as a tuple
  of float tuples, in input order.
- An empty batch returns ``()`` and sends nothing.
- An answer with another number of vectors than texts, or a vector that is not 768 long, is an
  ``LlmOutputError`` (the model is not the one the vector table was built for).
- Connection errors, timeouts and non-2xx answers raise ``LlmUnavailableError`` naming the host;
  with no ``base_url`` it says "not configured" and sends nothing.
"""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from paper_trail.adapters.ollama.embeddings import OllamaEmbeddings
from paper_trail.ports.llm import LlmOutputError, LlmUnavailableError
from tests.fakes.embeddings import EMBEDDING_DIMENSION, basis, hashed_vector
from tests.ollama_wire import BASE_URL, ollama_settings

TEXTS = ("ks org eggs 24ct", "kirkland organic eggs 24")


def _answer(*vectors: tuple[float, ...]) -> dict[str, Any]:
    return {"model": "embed-model:test", "embeddings": [list(vector) for vector in vectors]}


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


def test_s4_ac_08_one_request_per_batch_with_the_embed_model(router: respx.MockRouter) -> None:
    vectors = (basis(0), hashed_vector(TEXTS[1]))
    route = router.post("/api/embed").mock(return_value=httpx.Response(200, json=_answer(*vectors)))
    adapter = OllamaEmbeddings(ollama_settings(embed_model="embed-model:test", keep_alive="7m"))

    result = adapter.embed(TEXTS)

    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body == {"model": "embed-model:test", "input": list(TEXTS), "keep_alive": "7m"}
    assert result == vectors
    assert all(len(vector) == EMBEDDING_DIMENSION for vector in result)
    assert all(isinstance(value, float) for value in result[0])


def test_an_empty_batch_sends_nothing(router: respx.MockRouter) -> None:
    route = router.post("/api/embed")

    assert OllamaEmbeddings(ollama_settings()).embed([]) == ()
    assert route.call_count == 0


@pytest.mark.parametrize(
    "answer",
    [_answer(basis(0)), _answer(basis(0), (1.0, 0.0, 0.0)), {"embeddings": "nope"}],
    ids=["fewer-vectors", "wrong-dimension", "not-a-list"],
)
def test_an_answer_of_the_wrong_shape_is_an_output_error(
    router: respx.MockRouter, answer: dict[str, Any]
) -> None:
    router.post("/api/embed").mock(return_value=httpx.Response(200, json=answer))

    with pytest.raises(LlmOutputError):
        OllamaEmbeddings(ollama_settings()).embed(TEXTS)


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(500, json={"error": "model not loaded"}),
        httpx.Response(404, json={"error": 'model "nomic-embed-text" not found'}),
    ],
    ids=["refused", "timeout", "500", "404"],
)
def test_s4_ac_08_unavailable_errors_name_the_host(
    router: respx.MockRouter, outcome: Exception | httpx.Response
) -> None:
    route = router.post("/api/embed")
    if isinstance(outcome, httpx.Response):
        route.mock(return_value=outcome)
    else:
        route.mock(side_effect=outcome)

    with pytest.raises(LlmUnavailableError, match=r"llm-host\.test:11434"):
        OllamaEmbeddings(ollama_settings()).embed(TEXTS)


def test_not_configured_sends_nothing(router: respx.MockRouter) -> None:
    route = router.post("/api/embed")

    with pytest.raises(LlmUnavailableError, match="not configured"):
        OllamaEmbeddings(ollama_settings(base_url=None)).embed(TEXTS)

    assert route.call_count == 0


def test_the_request_timeout_comes_from_settings(router: respx.MockRouter) -> None:
    route = router.post("/api/embed").mock(
        return_value=httpx.Response(200, json=_answer(basis(0), basis(1)))
    )

    OllamaEmbeddings(ollama_settings(request_timeout_s=42.0)).embed(TEXTS)

    timeouts = route.calls.last.request.extensions["timeout"]
    assert timeouts["read"] == 42.0


# --- slice C review note 4 (pinned in docs/tests/agent-core-D.md) --------------------------------


def _with(index: int, value: object) -> list[object]:
    vector: list[object] = list(basis(0))
    vector[index] = value
    return vector


@pytest.mark.parametrize(
    "bad",
    ["0.5", None, [0.5], {"x": 0.5}, True, False],
    ids=["string", "null", "list", "object", "true", "false"],
)
def test_review_note_4_an_element_that_is_not_a_number_is_an_output_error(
    router: respx.MockRouter, bad: object
) -> None:
    """Every element must be a JSON number: a string, null, a container or a boolean (which
    Python would read as 1.0 or 0.0) is the model answering something else, not a vector."""
    answer = {"model": "embed-model:test", "embeddings": [_with(3, bad), list(basis(1))]}
    router.post("/api/embed").mock(return_value=httpx.Response(200, json=answer))

    with pytest.raises(LlmOutputError):
        OllamaEmbeddings(ollama_settings()).embed(TEXTS)


def test_review_note_4_integers_are_numbers_and_come_back_as_floats(
    router: respx.MockRouter,
) -> None:
    first = [0] * EMBEDDING_DIMENSION
    first[0] = 1
    answer = {"model": "embed-model:test", "embeddings": [first, list(basis(1))]}
    router.post("/api/embed").mock(return_value=httpx.Response(200, json=answer))

    result = OllamaEmbeddings(ollama_settings()).embed(TEXTS)

    assert result[0] == basis(0)
    assert all(type(value) is float for value in result[0])
