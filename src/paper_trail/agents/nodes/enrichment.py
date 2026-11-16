"""``enrich`` (enrichment role): what to describe for the products this receipt created
(barcode-lookup plan 4.2, ADR-0018; claude-extraction plan D3, V8).

Only the products ``save_matches`` created for this receipt's lines are described, each once,
from the first line linked to it. When matching named one from a source entry (PLU), its
description comes from that entry; ``commit_enrichment`` writes it with
``describe_from_catalog``, which keeps a person's fields and applies a person's category for
the line at that store. Every created product is also a target for the lookups and the guess
(``ProductEnricher``): its printed text, the store, the printed department heading and the code
matching read. The node only reads and asks nothing; the supervisor's ``commit_enrichment``
does the rest.
"""

from collections.abc import Mapping
from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import extraction_reading, saved_draft
from paper_trail.agents.json_values import as_dict, as_int, as_str
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.application.payloads import CatalogEntryPayload, catalog_fields
from paper_trail.domain.enums import CodeScheme
from paper_trail.domain.models import LineItem
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import normalize_store_name
from paper_trail.domain.rules.readings import reading_departments


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
        departments = (
            reading_departments(extraction_reading(state)) if state.get("draft") is None else {}
        )
        matches = {as_int(match["position"]): match for match in state.get("matches", [])}
        enrichments: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        for product_id, (line, entry) in sources.items():
            targets.append(
                _target(
                    product_id,
                    line,
                    store_name=draft.store_name,
                    store_key=store_key,
                    departments=departments,
                    matches=matches,
                )
            )
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
        return {"enrichments": enrichments, "targets": targets}

    return enrich


def _target(  # noqa: PLR0913 - one product and what its line says about it
    product_id: int,
    line: LineItem,
    *,
    store_name: str,
    store_key: str,
    departments: Mapping[int, str],
    matches: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """The ``EnrichTarget`` of a created product, as JSON."""
    match = matches.get(line.position, {})
    ref = match.get("code")
    code = as_dict(ref) if isinstance(ref, dict) else None
    identity = None if code is None else as_str(code["code"])
    printed = match.get("printed_code")
    gtin = code is not None and code.get("scheme") == CodeScheme.GTIN.value
    return {
        "product_id": product_id,
        "printed_text": mask_pans(line.raw_text).text,
        "store_name": mask_pans(store_name).text,
        "store_key": store_key,
        "department": departments.get(line.position),
        "code": printed if isinstance(printed, str) else identity,
        "gtin": identity if gtin else None,
    }
