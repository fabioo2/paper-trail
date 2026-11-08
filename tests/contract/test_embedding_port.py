"""EmbeddingPort contract, for the fake and the Ollama adapter (PLAN 6, S4-AC-08).

The graph and tool suites trust ``FakeEmbeddings`` to behave like ``OllamaEmbeddings``; this
suite holds both to the same observable behaviour. The adapter talks to a respx-mocked Ollama
that answers with the fake's own vectors, so no request leaves the process.

Pinned (docs/tests/agent-core-C.md): ``paper_trail.ports.embeddings.EmbeddingPort`` has
``embed(texts: Sequence[str]) -> tuple[tuple[float, ...], ...]``: one 768-long vector per text,
in order, the same text giving the same vector; ``()`` for no texts; ``LlmUnavailableError``
(``paper_trail.ports.llm``) when the host cannot be reached, so a GPU that drops mid-run stops
the run with its checkpoint instead of failing the receipt (S3-AC-10).
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
import respx

from paper_trail.adapters.ollama.embeddings import OllamaEmbeddings
from paper_trail.ports.embeddings import EmbeddingPort
from paper_trail.ports.llm import LlmUnavailableError
from tests.fakes.embeddings import EMBEDDING_DIMENSION, FakeEmbeddings
from tests.ollama_wire import BASE_URL, ollama_settings

TEXTS = ("ks org eggs 24ct", "kirkland organic eggs 24", "ks sourdough")


@dataclass
class Backend:
    name: str
    router: respx.MockRouter | None

    def healthy(self) -> EmbeddingPort:
        fake = FakeEmbeddings()
        if self.router is None:
            return fake

        def answer(request: httpx.Request) -> httpx.Response:
            texts = json.loads(request.content)["input"]
            vectors = [list(vector) for vector in fake.embed(texts)]
            return httpx.Response(200, json={"model": "m", "embeddings": vectors})

        self.router.post("/api/embed").mock(side_effect=answer)
        return OllamaEmbeddings(ollama_settings())

    def unreachable(self) -> EmbeddingPort:
        if self.router is None:
            return FakeEmbeddings(error=LlmUnavailableError("cannot reach llm-host.test:11434"))
        self.router.post("/api/embed").mock(side_effect=httpx.ConnectError("refused"))
        return OllamaEmbeddings(ollama_settings())


@pytest.fixture(params=["fake", "ollama"])
def backend(request: pytest.FixtureRequest) -> Iterator[Backend]:
    if request.param == "fake":
        yield Backend("fake", None)
        return
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield Backend("ollama", router)


def test_s4_ac_08_one_768_long_vector_per_text_in_order(backend: Backend) -> None:
    embeddings = backend.healthy()

    vectors = embeddings.embed(TEXTS)

    assert len(vectors) == len(TEXTS)
    assert all(len(vector) == EMBEDDING_DIMENSION for vector in vectors)
    assert embeddings.embed(TEXTS[1:2]) == (vectors[1],)
    assert len(set(vectors)) == len(TEXTS)


def test_no_texts_no_vectors(backend: Backend) -> None:
    assert backend.healthy().embed([]) == ()


def test_s4_ac_08_an_unreachable_host_is_unavailable(backend: Backend) -> None:
    with pytest.raises(LlmUnavailableError):
        backend.unreachable().embed(TEXTS)
