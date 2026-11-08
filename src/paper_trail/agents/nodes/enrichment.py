"""``enrich`` (enrichment role): describe the products this receipt created (PLAN 6, AC-D-02).

Only the products ``save_matches`` created for this receipt's lines are described, each once,
in line order, from the first line linked to it: products found by memory, alias, vector or
confirmation are already known, and a product a person named in a review is theirs. One
``enrichment/v1`` request per product gives its full name, brand, size, one category of the
fixed list and tags. An answer that never validates (an invented category included, S4-AC-05)
or that is not confident enough leaves the product as it is: it is not asked again. The node
only reads; the supervisor's ``commit_enrichment`` saves the answers, and the tool applies a
person's category for the line at that store over the model's.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import saved_draft
from paper_trail.agents.prompts import RenderedPrompt
from paper_trail.agents.schemas import EnrichmentOutput
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.models import LineItem
from paper_trail.domain.rules.normalization import normalize_store_name, split_item_code
from paper_trail.ports.llm import LlmOutputError, LlmRequest

ENRICHMENT_PROMPT = "enrichment"


def enrich_node(deps: AgentDeps) -> Node:
    settings = deps.settings
    prompt = deps.prompts.load(
        ENRICHMENT_PROMPT, settings.prompts.enrichment_version, examples=settings.prompts.examples
    )

    def enrich(state: ReceiptState) -> ReceiptState:
        draft = saved_draft(deps, state)
        store_key = normalize_store_name(draft.store_name)
        lines = {line.position: line for line in draft.items}
        sources: dict[int, LineItem] = {}  # product id -> the first line linked to it
        for created in state.get("created", []):
            sources.setdefault(int(created["product_id"]), lines[int(created["position"])])
        enrichments: list[dict[str, Any]] = []
        for product_id, line in sources.items():
            answer = _describe(deps, prompt, line)
            if answer is not None:
                enrichments.append(
                    {
                        "product_id": product_id,
                        "alias": line.raw_text,
                        "store_key": store_key,
                        "enrichment": answer.enrichment_json(),
                    }
                )
        return {"enrichments": enrichments}

    return enrich


def _describe(deps: AgentDeps, prompt: RenderedPrompt, line: LineItem) -> EnrichmentOutput | None:
    """The model's confident description of the line's product, else None. The line is sent
    without its leading item code, which the model would read as a size (``1281 1% MILK``)."""
    _, text = split_item_code(line.raw_text)
    request = LlmRequest(
        model=deps.settings.ollama.text_model,
        tag=prompt.tag,
        messages=prompt.messages(f"line: {text}\ndescription: {line.description}", ()),
        temperature=0.0,
    )
    try:
        answer = deps.llm.complete_structured(request, EnrichmentOutput).value
    except LlmOutputError:
        return None
    return answer if answer.confidence >= deps.settings.graph.min_confidence else None
