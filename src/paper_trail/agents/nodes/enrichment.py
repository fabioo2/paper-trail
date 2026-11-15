"""``enrich`` (enrichment role): describe the products this receipt created from the catalog
entry matching found for them, asking no model (barcode-lookup plan 4.2, ADR-0018).

Only the products ``save_matches`` created for this receipt's lines are described, each once,
from the first line linked to it, and only when matching named it from a source entry (PLU): a
printed-text product stays as it is, and the code is not looked up again (a second lookup
would find the product just created). The node only reads; the supervisor's
``commit_enrichment`` writes each description with ``describe_from_catalog``, which keeps a
person's fields and applies a person's category for the line at that store.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import saved_draft
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.application.payloads import CatalogEntryPayload, catalog_fields
from paper_trail.domain.models import LineItem
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import normalize_store_name


def enrich_node(deps: AgentDeps) -> Node:
    def enrich(state: ReceiptState) -> ReceiptState:
        draft = saved_draft(deps, state)
        store_key = normalize_store_name(mask_pans(draft.store_name).text)
        lines = {line.position: line for line in draft.items}
        sources: dict[int, tuple[LineItem, Any]] = {}  # product id -> its first line and entry
        for created in state.get("created", []):
            sources.setdefault(
                int(created["product_id"]),
                (lines[int(created["position"])], created.get("catalog")),
            )
        enrichments: list[dict[str, Any]] = []
        for product_id, (line, entry) in sources.items():
            if entry is None:
                continue
            fields, field_sources = catalog_fields(CatalogEntryPayload.model_validate(entry))
            if not field_sources:
                continue
            enrichments.append(
                {
                    "product_id": product_id,
                    "fields": fields.model_dump(mode="json", exclude_none=True),
                    "sources": {key: source.value for key, source in field_sources.items()},
                    "alias": mask_pans(line.raw_text).text,
                    "store_key": store_key,
                }
            )
        return {"enrichments": enrichments}

    return enrich
