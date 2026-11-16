"""A product database for tests (implements ``paper_trail.ports.lookups.ProductLookup``): a
canned answer per code (a hit, None for not found, or an exception to raise), every code asked
recorded in ``asked``. A code with no answer is not found. ``during`` runs before each answer
(a person's write landing while the request is in flight)."""

from collections.abc import Callable, Mapping

from paper_trail.ports.lookups import LookupHit, LookupSource

type Answer = LookupHit | BaseException | None


class FakeLookup:
    def __init__(
        self,
        source: LookupSource,
        answers: Mapping[str, Answer] | None = None,
        *,
        during: Callable[[], None] | None = None,
    ) -> None:
        self._source: LookupSource = source
        self.answers: dict[str, Answer] = dict(answers or {})
        self.asked: list[str] = []
        self.during = during

    @property
    def source(self) -> LookupSource:
        return self._source

    def lookup(self, code: str) -> LookupHit | None:
        self.asked.append(code)
        if self.during is not None:
            self.during()
        answer = self.answers.get(code)
        if isinstance(answer, BaseException):
            raise answer
        return answer
