"""``match`` (matching role): find each saved line's product, reading only (PLAN 6, AC-C-02).

For every ITEM line, in order, whose alias (``normalize_alias`` of its raw text) names a
product rather than a kind of line (``is_generic_alias``):

1. ``product_lookup`` at the receipt's store: a person's correction, then an exact alias;
2. ``product_similarity_search`` for the three nearest products: the nearest within
   ``MATCHING__AUTO_LINK_MAX_DISTANCE`` is linked; when the nearest is within
   ``MATCHING__REVIEW_MAX_DISTANCE`` the model is asked once (``matching/v1``) whether the line
   is one of the candidates in that band, and a confident "same" with an offered id links it;
3. anything else is a new product named after the line's description.

DEPOSIT, FEE and DISCOUNT lines are never matched, and neither is a line a person named a
product for in a review answer (``persist`` linked it: a person outranks memory, aliases and
vectors). The decisions go into the state; the supervisor's ``commit_matches`` writes them.
"""

from dataclasses import dataclass
from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import saved_draft
from paper_trail.agents.json_values import as_dict, as_float, as_int, as_list, as_str
from paper_trail.agents.prompts import RenderedPrompt
from paper_trail.agents.schemas import MatchDecision
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.enums import AgentRole, LineKind, MatchSource
from paper_trail.domain.models import JsonValue, LineItem
from paper_trail.domain.rules.normalization import (
    is_generic_alias,
    normalize_alias,
    normalize_store_name,
)
from paper_trail.ports.llm import LlmOutputError, LlmRequest

MATCHING_PROMPT = "matching"
CANDIDATES = 3  # PLAN 6: the model chooses among the top three


@dataclass(frozen=True, slots=True)
class _Candidate:
    product_id: int
    name: str
    distance: float

    @classmethod
    def of(cls, entry: JsonValue) -> "_Candidate":
        candidate = as_dict(entry)
        product = as_dict(candidate["product"])
        return cls(
            as_int(product["id"]),
            as_str(product["canonical_name"]),
            as_float(candidate["distance"]),
        )


def match_node(deps: AgentDeps) -> Node:
    settings = deps.settings
    prompt = deps.prompts.load(
        MATCHING_PROMPT, settings.prompts.matching_version, examples=settings.prompts.examples
    )

    def match(state: ReceiptState) -> ReceiptState:
        draft = saved_draft(deps, state)
        store_key = normalize_store_name(draft.store_name)
        matches: list[dict[str, Any]] = []
        named = draft.product_names  # linked by a person's review answer
        for line in sorted(draft.items, key=lambda item: item.position):
            alias = normalize_alias(line.raw_text)
            if line.position in named:
                continue
            if line.kind is LineKind.ITEM and not is_generic_alias(alias):
                matches.append(_decide(deps, prompt, line, alias, store_key))
        return {"matches": matches}

    return match


def _decide(
    deps: AgentDeps, prompt: RenderedPrompt, line: LineItem, alias: str, store_key: str
) -> dict[str, Any]:
    found = deps.tools.call(
        AgentRole.MATCHING, "product_lookup", {"alias": alias, "store_key": store_key}
    )
    if found["product"] is not None:
        product_id = as_int(as_dict(found["product"])["id"])
        return _linked(line, MatchSource(as_str(found["source"])), product_id)
    result = deps.tools.call(
        AgentRole.MATCHING, "product_similarity_search", {"text": alias, "k": CANDIDATES}
    )
    candidates = [_Candidate.of(entry) for entry in as_list(result["candidates"])]
    limits = deps.settings.matching
    if candidates and candidates[0].distance <= limits.auto_link_max_distance:
        return _linked(line, MatchSource.VECTOR, candidates[0].product_id)
    in_band = [c for c in candidates if c.distance <= limits.review_max_distance]
    confirmed = _confirm(deps, prompt, line, in_band) if in_band else None
    if confirmed is not None:
        return _linked(line, MatchSource.LLM, confirmed)
    return {
        "position": line.position,
        "source": MatchSource.NEW.value,
        "product_id": None,
        "new_product_name": line.description,
    }


def _linked(line: LineItem, source: MatchSource, product_id: int) -> dict[str, Any]:
    return {
        "position": line.position,
        "source": source.value,
        "product_id": product_id,
        "new_product_name": None,
    }


def _confirm(
    deps: AgentDeps, prompt: RenderedPrompt, line: LineItem, candidates: list[_Candidate]
) -> int | None:
    """The offered product the model confidently says the line is, else None. An answer that
    never validates, or that is about another line, counts as "different": it is not asked
    again."""
    request = LlmRequest(
        model=deps.settings.ollama.text_model,
        tag=prompt.tag,
        messages=prompt.messages(_question(line, candidates), ()),
        temperature=0.0,
    )
    try:
        answer = deps.llm.complete_structured(request, MatchDecision).value
    except LlmOutputError:
        return None
    offered = {candidate.product_id for candidate in candidates}
    sure = answer.confidence >= deps.settings.graph.min_confidence
    about_this_line = answer.line_position == line.position
    if answer.decision == "same" and answer.product_id in offered and sure and about_this_line:
        return answer.product_id
    return None


def _question(line: LineItem, candidates: list[_Candidate]) -> str:
    """The line and its candidates: the data block of the matching prompt."""
    offered = "\n".join(f"- id {c.product_id}: {c.name}" for c in candidates)
    return (
        f"line {line.position}: {line.raw_text}\n"
        f"description: {line.description}\n"
        f"candidates:\n{offered}"
    )
