"""One declaration per tool, with the roles allowed to call it (PLAN 7, ADR-0013).

A call is checked in a fixed order: the tool exists, the role may call it (whatever the
payload), the payload validates against the tool's input model, the handler runs, and its
result validates against the tool's output model. Denials are logged and raised, never
swallowed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.ports.tools import (
    ToolDescription,
    ToolInputError,
    ToolNotPermittedError,
    ToolOutputError,
    UnknownToolError,
)
from paper_trail.tools.handlers import (
    ToolServices,
    check_arithmetic_tool,
    ocr_read,
    open_review,
    product_lookup,
    product_similarity_search,
    save_enrichment,
    save_matches,
    save_receipt,
)
from paper_trail.tools.models import (
    CheckArithmeticInput,
    CheckArithmeticOutput,
    OcrReadInput,
    OcrReadOutput,
    OpenReviewInput,
    OpenReviewOutput,
    ProductLookupInput,
    ProductLookupOutput,
    SaveEnrichmentInput,
    SaveEnrichmentOutput,
    SaveMatchesInput,
    SaveMatchesOutput,
    SaveReceiptInput,
    SaveReceiptOutput,
    SimilaritySearchInput,
    SimilaritySearchOutput,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolSpec[I: BaseModel, O: BaseModel]:
    name: str
    description: str
    input_model: type[I]
    output_model: type[O]
    handler: Callable[[I], O]
    roles: frozenset[AgentRole]


type AnySpec = ToolSpec[Any, Any]


class RoleToolbox:
    """The tools one role may call."""

    def __init__(self, role: AgentRole, specs: Mapping[str, AnySpec]) -> None:
        self._role = role
        self._specs = specs

    @property
    def specs(self) -> tuple[AnySpec, ...]:
        """The role's tools, sorted by name."""
        return tuple(spec for _, spec in sorted(self._specs.items()) if self._role in spec.roles)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)

    def describe(self) -> tuple[ToolDescription, ...]:
        return tuple(ToolDescription(spec.name, spec.description) for spec in self.specs)

    def call(self, name: str, payload: Mapping[str, JsonValue]) -> BaseModel:
        spec = self._specs.get(name)
        if spec is None:
            msg = f"unknown tool {name!r}"
            raise UnknownToolError(msg)
        if self._role not in spec.roles:
            log.warning("tool.denied", role=self._role.value, tool=name)
            raise ToolNotPermittedError(self._role, name)
        try:
            request = spec.input_model.model_validate(payload)
        except ValidationError as error:
            raise ToolInputError(_problems(name, "input", error)) from None
        return _checked_output(spec, spec.handler(request))


class ToolRegistry:
    def __init__(self, specs: tuple[AnySpec, ...]) -> None:
        by_name: dict[str, AnySpec] = {}
        for spec in specs:
            if spec.name in by_name:
                msg = f"two tools are named {spec.name!r}"
                raise ValueError(msg)
            if not spec.roles:
                msg = f"no role may call tool {spec.name!r}"
                raise ValueError(msg)
            by_name[spec.name] = spec
        self.specs = specs
        self._by_name = by_name

    def for_role(self, role: AgentRole) -> RoleToolbox:
        return RoleToolbox(role, self._by_name)


def _checked_output(spec: AnySpec, output: object) -> BaseModel:
    """The handler's result, revalidated: a tool must return its declared output model."""
    if not isinstance(output, spec.output_model):
        msg = f"{spec.name}: returned {type(output).__name__}, not {spec.output_model.__name__}"
        raise ToolOutputError(msg)
    try:
        checked: BaseModel = spec.output_model.model_validate(dict(output))
    except ValidationError as error:
        raise ToolOutputError(_problems(spec.name, "output", error)) from None
    return checked


def _problems(tool: str, what: str, error: ValidationError) -> str:
    """Which fields are wrong and why, without echoing the (possibly sensitive) values."""
    problems = "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or '(payload)'}: {detail['msg']}"
        for detail in error.errors()
    )
    return f"{tool}: invalid {what}: {problems}"


def build_tool_registry(services: ToolServices) -> ToolRegistry:
    """Every tool with its allowlist. Specialists only read; every write is a supervisor's."""
    return ToolRegistry(
        (
            ToolSpec(
                name="ocr_read",
                description="OCR a stored receipt image; card numbers come back masked.",
                input_model=OcrReadInput,
                output_model=OcrReadOutput,
                handler=partial(ocr_read, services),
                roles=frozenset({AgentRole.EXTRACTION}),
            ),
            ToolSpec(
                name="check_arithmetic",
                description="Check that lines, subtotal, tax and total of a receipt add up.",
                input_model=CheckArithmeticInput,
                output_model=CheckArithmeticOutput,
                handler=partial(check_arithmetic_tool, services),
                roles=frozenset({AgentRole.VALIDATION}),
            ),
            ToolSpec(
                name="save_receipt",
                description="Save a draft as the contents of a receipt being processed.",
                input_model=SaveReceiptInput,
                output_model=SaveReceiptOutput,
                handler=partial(save_receipt, services),
                roles=frozenset({AgentRole.SUPERVISOR}),
            ),
            ToolSpec(
                name="open_review",
                description="Park a receipt for a person with an open review item.",
                input_model=OpenReviewInput,
                output_model=OpenReviewOutput,
                handler=partial(open_review, services),
                roles=frozenset({AgentRole.SUPERVISOR}),
            ),
            ToolSpec(
                name="product_lookup",
                description=(
                    "Find the product a receipt line's text names at a store: a person's"
                    " correction first, then an exact alias."
                ),
                input_model=ProductLookupInput,
                output_model=ProductLookupOutput,
                handler=partial(product_lookup, services),
                roles=frozenset({AgentRole.MATCHING, AgentRole.ENRICHMENT}),
            ),
            ToolSpec(
                name="product_similarity_search",
                description="The k products whose embeddings are nearest a text, nearest first.",
                input_model=SimilaritySearchInput,
                output_model=SimilaritySearchOutput,
                handler=partial(product_similarity_search, services),
                roles=frozenset({AgentRole.MATCHING}),
            ),
            ToolSpec(
                name="save_matches",
                description=(
                    "Link a saved receipt's lines to their products (new ones created), with"
                    " aliases, vectors and prices, in one transaction."
                ),
                input_model=SaveMatchesInput,
                output_model=SaveMatchesOutput,
                handler=partial(save_matches, services),
                roles=frozenset({AgentRole.SUPERVISOR}),
            ),
            ToolSpec(
                name="save_enrichment",
                description=(
                    "Describe a product (name, brand, size, category, tags) and replace its"
                    " embedding; a person's category for the line at that store wins."
                ),
                input_model=SaveEnrichmentInput,
                output_model=SaveEnrichmentOutput,
                handler=partial(save_enrichment, services),
                roles=frozenset({AgentRole.SUPERVISOR}),
            ),
        )
    )
