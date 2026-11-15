"""``match`` (matching role): find each saved line's product by its code or a person's word,
reading only and asking no model (barcode-lookup plan 4.1, ADR-0018).

For every ITEM line, in order, whose alias (``normalize_alias`` of its raw text) names a
product rather than a kind of line (``is_generic_alias``):

- a line that starts with a product code (``catalog_lookup``): the code's product when the code
  is ours (``catalog``); else a person's correction for the line at that store (``memory``,
  which teaches the code); else a new product named by the source that knows the code (PLU),
  else by its printed text. A shared text never links a coded line (an alias is not used).
- any other line, and every line with ``CATALOG__ENABLED`` off: ``product_lookup`` at the
  receipt's store (a person's correction, then the store's exact alias), else a new product
  named by its printed text.

Each line is read as it is stored, card numbers masked: a code that masking turned into a card
number's last four digits is no code, and the line goes the uncoded way. DEPOSIT, FEE and
DISCOUNT lines are never matched, and neither is a line a person named a
product for in a review answer (``persist`` linked it). The decisions go into the state, with
one NOTE counting them by source; the supervisor's ``commit_matches`` writes them.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import saved_draft
from paper_trail.agents.json_values import as_dict, as_int, as_str
from paper_trail.agents.state import STATE_VERSION, ReceiptState
from paper_trail.agents.tracing import Node, match_counts_summary
from paper_trail.application.payloads import entry_product_name
from paper_trail.domain.enums import AgentRole, CodeScheme, FieldSource, LineKind, MatchSource
from paper_trail.domain.models import JsonValue, LineItem
from paper_trail.domain.rules.item_codes import parse_item_code, printed_name
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import (
    is_generic_alias,
    normalize_alias,
    normalize_store_name,
)
from paper_trail.domain.trace import TraceKind

NOTE = "matching"


def match_node(deps: AgentDeps) -> Node:
    catalog = deps.settings.catalog.enabled

    def match(state: ReceiptState) -> ReceiptState:
        draft = saved_draft(deps, state)
        store_key = normalize_store_name(mask_pans(draft.store_name).text)
        matches: list[dict[str, Any]] = []
        named = draft.product_names  # linked by a person's review answer
        for read in sorted(draft.items, key=lambda item: item.position):
            if read.position in named:
                continue
            line = replace(read, raw_text=mask_pans(read.raw_text).text)
            if line.kind is LineKind.ITEM and not is_generic_alias(normalize_alias(line.raw_text)):
                parsed = parse_item_code(line.raw_text, store_key) if catalog else None
                coded = parsed is not None and parsed.identity_at(store_key) is not None
                matches.append(
                    _by_code(deps, line, store_key) if coded else _by_text(deps, line, store_key)
                )
        if matches:
            counts = _counts(matches)
            deps.tracer.emit(
                AgentRole.MATCHING,
                TraceKind.NOTE,
                NOTE,
                match_counts_summary(counts),
                data=counts,
            )
        return {"matches": matches, "state_version": STATE_VERSION}

    return match


def _by_code(deps: AgentDeps, line: LineItem, store_key: str) -> dict[str, Any]:
    """The decision for a line that starts with a product code: its code's product, a
    person's correction, a source entry, else its printed text."""
    found = deps.tools.call(
        AgentRole.MATCHING, "catalog_lookup", {"raw_text": line.raw_text, "store_key": store_key}
    )
    code = as_dict(found["code"])
    scheme = as_str(code["scheme"])
    ref = {
        "scheme": scheme,
        "store_key": store_key if scheme == CodeScheme.STORE.value else "",
        "code": as_str(code["identity"]),
    }
    if found["product"] is not None:
        product_id = as_int(as_dict(found["product"])["id"])
        return _linked(line, MatchSource.CATALOG, product_id) | {"code": ref}
    remembered = _lookup(deps, line, store_key)
    if remembered is not None and remembered[0] is MatchSource.MEMORY:
        return _linked(line, MatchSource.MEMORY, remembered[1]) | {"code": ref}
    entry = found["entry"]
    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
        brand = entry.get("brand")
        name = entry_product_name(as_str(entry["name"]), brand if isinstance(brand, str) else None)
        return _new(line, name, FieldSource.PLU) | {"code": ref, "catalog": entry}
    return _new(line, printed_name(line.raw_text), FieldSource.RECEIPT_TEXT) | {"code": ref}


def _by_text(deps: AgentDeps, line: LineItem, store_key: str) -> dict[str, Any]:
    remembered = _lookup(deps, line, store_key)
    if remembered is not None:
        return _linked(line, *remembered)
    return _new(line, printed_name(line.raw_text), FieldSource.RECEIPT_TEXT)


def _lookup(deps: AgentDeps, line: LineItem, store_key: str) -> tuple[MatchSource, int] | None:
    """A person's correction or the store's exact alias for the line's text."""
    found = deps.tools.call(
        AgentRole.MATCHING, "product_lookup", {"alias": line.raw_text, "store_key": store_key}
    )
    if found["product"] is None:
        return None
    return MatchSource(as_str(found["source"])), as_int(as_dict(found["product"])["id"])


def _linked(line: LineItem, source: MatchSource, product_id: int) -> dict[str, Any]:
    return {
        "position": line.position,
        "source": source.value,
        "product_id": product_id,
        "new_product_name": None,
    }


def _new(line: LineItem, name: str, name_source: FieldSource) -> dict[str, Any]:
    return {
        "position": line.position,
        "source": MatchSource.NEW.value,
        "product_id": None,
        "new_product_name": name,
        "name_source": name_source.value,
    }


def _counts(matches: list[dict[str, Any]]) -> dict[str, JsonValue]:
    """The NOTE's data: per source, CATALOG by code scheme and NEW by name source."""
    counts: dict[str, JsonValue] = {}
    for source in MatchSource:
        chosen = [match for match in matches if match["source"] == source.value]
        if not chosen:
            continue
        if source is MatchSource.CATALOG:
            counts[source.value] = _split(match["code"]["scheme"] for match in chosen)
        elif source is MatchSource.NEW:
            counts[source.value] = _split(match["name_source"] for match in chosen)
        else:
            counts[source.value] = len(chosen)
    return counts


def _split(keys: Iterable[str]) -> dict[str, JsonValue]:
    split: dict[str, JsonValue] = {}
    for key, count in Counter(keys).items():
        split[key] = count
    return split
