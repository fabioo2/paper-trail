"""Matching with alias and correction memory, through the whole graph (PLAN 6, agent core
slice C): S4-AC-01, S4-AC-02, S4-AC-03, S4-AC-04 (minus enrichment, slice D), S4-AC-07,
AC-C-01, AC-C-02 and AC-C-03, with the scripted LLM and fake embeddings.

In-process tools; the MCP transport is compared in ``test_transport_equivalence.py`` (S5-AC-04).

Pinned (docs/tests/agent-core-C.md):
- Graph: ``persist -> match -> commit_matches -> END``. ``match`` is the MATCHING agent and only
  reads (``product_lookup``, ``product_similarity_search``); ``commit_matches`` is the
  SUPERVISOR and writes everything with one ``save_matches`` call, also when no line is matched
  (an empty ``decisions`` list).
- For each ITEM line, in position order, whose alias (``normalize_alias(raw_text)``) is not
  generic (``is_generic_alias``): ``product_lookup(alias, store_key)``, where ``store_key`` is
  ``normalize_store_name(store_name)``; a product -> its source (``memory`` or ``alias``).
  Otherwise ``product_similarity_search(alias, k=3)``: the nearest candidate at distance
  <= ``MATCHING__AUTO_LINK_MAX_DISTANCE`` -> ``vector``; else, when the nearest is at most
  ``MATCHING__REVIEW_MAX_DISTANCE``, one LLM confirmation (``matching/v1``) among the candidates
  within that distance (the top 3): ``same`` with one of their ids and confidence >=
  ``GRAPH__MIN_CONFIDENCE`` -> that product, source ``llm``; anything else (``different``, an id
  that was not offered, low confidence, an answer that never validates) -> a new product, source
  ``new``, named after the line's ``description``. No candidates -> ``new``. DEPOSIT, FEE and
  DISCOUNT lines and generic aliases get no tool call and stay unlinked.
- The confirmation request: ``settings.ollama.text_model``, temperature 0, tag
  ``matching/v1``; its final user message holds the line's raw text and each offered
  candidate's id and canonical name inside ``<receipt_data>``. The LLM trace event's agent is
  MATCHING.
- The ``save_matches`` TOOL event's ``data["output"]["lines"]`` gives each linked line's
  ``source`` (the trace shows the source per line). Its summary starts with the count per
  source, in ``MatchSource`` order (``memory 1 · alias 2``), so the terminal's 42-column result
  cell shows them; then the new products and prices.
"""

from typing import Any

import pytest

from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import AgentRole, MatchSource, ReceiptStatus
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.unit_of_work import UnitOfWork
from tests.agent_harness import (
    TEXT_MODEL,
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_ALIASES,
    COSTCO_OCR,
    EGGS_ALIAS,
    KIRKLAND_EGGS,
    KIRKLAND_EGGS_ALIAS,
    STORE_KEY,
    STORE_NAME,
    WALMART,
    WALMART_KEY,
    extraction,
    inconsistent_extraction,
    match_decision,
    mixed_lines_extraction,
    one_line_extraction,
)
from tests.factories import product, receipt_draft
from tests.fakes.embeddings import FakeEmbeddings, at_distance, basis
from tests.fakes.llm import Reply, ScriptedLlm
from tests.storage import in_unit
from tests.support import require_id

LLM = TraceKind.LLM
TOOL = TraceKind.TOOL
NEW, ALIAS, VECTOR = MatchSource.NEW, MatchSource.ALIAS, MatchSource.VECTOR
MATCHING_TAG = "matching/v1"
DESCRIPTIONS = ["Kirkland Organic Eggs 24 ct", "Kirkland 2% Milk 4 L", "Kirkland Sourdough Bread"]


def _harness(*replies: Reply, table: dict[str, tuple[float, ...]] | None = None) -> Harness:
    return build_harness(ScriptedLlm(*replies), embeddings=FakeEmbeddings(table))


def _completed(outcome: ProcessOutcome) -> int:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    return outcome.receipt_id


def _costco(harness: Harness, name: str = "costco.txt") -> int:
    return _completed(harness.process_text(COSTCO_OCR, name))


def _walmart_eggs(harness: Harness) -> int:
    return _completed(harness.process_text(f"{WALMART}\n{KIRKLAND_EGGS} 8.99", "walmart.txt"))


def _matching_requests(harness: Harness) -> list[int]:
    return [i for i, request in enumerate(harness.llm.requests) if request.tag == MATCHING_TAG]


def _save_matches_event(harness: Harness, receipt_id: int) -> TraceEvent:
    saves = [
        event
        for event in harness.events(receipt_id)
        if event.kind is TOOL and event.name == "save_matches"
    ]
    return saves[-1]


def _traced_sources(event: TraceEvent) -> dict[int, str]:
    output = event.data["output"]
    assert isinstance(output, dict)
    lines = output["lines"]
    assert isinstance(lines, list)
    return {_int(line, "position"): str(_field(line, "source")) for line in lines}


def _field(line: JsonValue, key: str) -> JsonValue:
    assert isinstance(line, dict)
    return line[key]


def _int(line: JsonValue, key: str) -> int:
    value = _field(line, key)
    assert isinstance(value, int)
    return value


def _seed(harness: Harness, name: str, vector: tuple[float, ...]) -> int:
    def add(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(product(canonical_name=name)).id)
        uow.vectors.upsert(product_id, vector)
        return product_id

    return in_unit(harness.uow_factory, add)


# --- S4-AC-04 / AC-C-03 step 1: the first receipt creates products ----------------------------


def test_s4_ac_04_a_first_receipt_creates_a_product_per_line() -> None:
    harness = _harness(extraction())

    receipt_id = _costco(harness)

    stored = harness.receipt(receipt_id)
    assert harness.match_sources(receipt_id) == [NEW, NEW, NEW]
    product_ids = [require_id(item.product_id) for item in stored.items]
    assert len(set(product_ids)) == 3
    assert [harness.product(pid).canonical_name for pid in product_ids] == DESCRIPTIONS
    assert all(harness.has_vector(pid) for pid in product_ids)
    assert harness.embeddings.requests == [
        (EGGS_ALIAS,),
        (COSTCO_ALIASES[1],),
        (COSTCO_ALIASES[2],),
        COSTCO_ALIASES,
    ]
    assert _matching_requests(harness) == []
    for pid, alias in zip(product_ids, COSTCO_ALIASES, strict=True):
        assert harness.find_by_alias(alias, STORE_KEY) == harness.product(pid)
        (price,) = harness.price_history(pid)
        assert (price.receipt_id, price.store_key) == (receipt_id, STORE_KEY)


def test_the_graph_runs_match_then_commit_matches() -> None:
    harness = _harness(extraction())

    receipt_id = _costco(harness)

    assert harness.node_runs(receipt_id)[-3:] == ["persist", "match", "commit_matches"]
    lookup, search = (TOOL, "product_lookup"), (TOOL, "product_similarity_search")
    assert harness.steps(receipt_id)[-7:] == [
        lookup,
        search,
        lookup,
        search,
        lookup,
        search,
        (TOOL, "save_matches"),
    ]


def test_s4_ac_04_a_line_far_from_every_product_is_new() -> None:
    """Above REVIEW_MAX_DISTANCE: a new product, no confirmation, its vector stored."""
    harness = _harness(one_line_extraction("KS ORG EGGS 24CT"), table={EGGS_ALIAS: basis(0)})
    far = _seed(harness, "Something else", at_distance(0.6))

    receipt_id = _completed(harness.process_text("KS ORG EGGS 24CT 8.99"))

    (line,) = harness.receipt(receipt_id).items
    assert line.match_source is NEW
    assert line.product_id not in (None, far)
    assert harness.has_vector(require_id(line.product_id))
    assert _matching_requests(harness) == []


# --- S4-AC-01 and AC-C-03 step 2: the same text at the same store hits the alias ---------------


def test_s4_ac_01_an_alias_hit_calls_neither_embeddings_nor_the_model() -> None:
    harness = _harness(extraction(), extraction())
    first = _costco(harness, "a.txt")
    embeddings_before = harness.embeddings.calls

    second = _costco(harness, "b.txt")

    assert harness.match_sources(second) == [ALIAS, ALIAS, ALIAS]
    assert [item.product_id for item in harness.receipt(second).items] == [
        item.product_id for item in harness.receipt(first).items
    ]
    assert harness.embeddings.calls == embeddings_before
    assert _matching_requests(harness) == []
    assert harness.llm.calls == 2  # the two extractions only
    steps = harness.steps(second)
    assert (TOOL, "product_similarity_search") not in steps
    assert steps[-4:] == [(TOOL, "product_lookup")] * 3 + [(TOOL, "save_matches")]
    for item in harness.receipt(second).items:
        assert len(harness.price_history(require_id(item.product_id))) == 2


# --- S4-AC-02: a second store's spelling links through the vector index ------------------------


def test_s4_ac_02_close_embeddings_link_two_stores_to_one_product() -> None:
    table = {EGGS_ALIAS: basis(0), KIRKLAND_EGGS_ALIAS: at_distance(0.05)}
    walmart_reply = one_line_extraction(KIRKLAND_EGGS, store_name=WALMART)
    harness = _harness(extraction(), walmart_reply, table=table)
    costco = _costco(harness)
    eggs = require_id(harness.line(costco, 0).product_id)

    walmart = _walmart_eggs(harness)

    (line,) = harness.receipt(walmart).items
    assert (line.product_id, line.match_source) == (eggs, VECTOR)
    assert {price.store_key for price in harness.price_history(eggs)} == {STORE_KEY, WALMART_KEY}
    assert harness.find_by_alias(KIRKLAND_EGGS_ALIAS, WALMART_KEY) == harness.product(eggs)
    assert _matching_requests(harness) == []
    assert _traced_sources(_save_matches_event(harness, walmart)) == {0: "vector"}


# --- S4-AC-03: the confirmation band ------------------------------------------------------------


def _in_band(harness: Harness) -> list[int]:
    """Four products between the thresholds of the Walmart eggs (basis(0)), nearest first."""
    return [
        _seed(harness, f"Candidate {index}", at_distance(distance, toward=index))
        for index, distance in enumerate((0.18, 0.20, 0.22, 0.25), start=1)
    ]


def _band_harness(answer: Reply) -> Harness:
    return _harness(
        one_line_extraction(KIRKLAND_EGGS, store_name=WALMART),
        answer,
        table={KIRKLAND_EGGS_ALIAS: basis(0)},
    )


def test_s4_ac_03_same_links_after_exactly_one_confirmation_with_the_top_candidates() -> None:
    harness = _band_harness(match_decision("same", 2))
    candidates = _in_band(harness)
    assert candidates == [1, 2, 3, 4]  # a fresh database numbers products from 1

    receipt_id = _walmart_eggs(harness)

    (line,) = harness.receipt(receipt_id).items
    assert (line.product_id, line.match_source) == (2, MatchSource.LLM)
    (index,) = _matching_requests(harness)
    request = harness.llm.requests[index]
    assert request.model == TEXT_MODEL
    assert request.temperature == 0.0
    final = harness.llm.last_user_message(index)
    data = final.split("<receipt_data>", 1)[1].split("</receipt_data>", 1)[0]
    assert KIRKLAND_EGGS in data
    for offered in (1, 2, 3):
        assert str(offered) in data
        assert f"Candidate {offered}" in data
    assert "Candidate 4" not in final
    (event,) = [e for e in harness.events(receipt_id) if e.kind is LLM and e.name == MATCHING_TAG]
    assert event.agent is AgentRole.MATCHING
    assert _traced_sources(_save_matches_event(harness, receipt_id)) == {0: "llm"}


@pytest.mark.parametrize(
    "answer",
    [
        match_decision("different", None),
        match_decision("same", 4),  # Candidate 4 was not offered
        match_decision("same", 1, confidence=0.3),  # below GRAPH__MIN_CONFIDENCE
        "this is not JSON",
    ],
    ids=["different", "not-offered", "low-confidence", "unparsed"],
)
def test_s4_ac_03_anything_but_a_confident_same_creates_a_new_product(answer: Reply) -> None:
    harness = _band_harness(answer)
    candidates = _in_band(harness)

    receipt_id = _walmart_eggs(harness)

    (line,) = harness.receipt(receipt_id).items
    assert line.match_source is NEW
    assert line.product_id not in candidates
    assert len(_matching_requests(harness)) == 1  # one confirmation, never retried
    assert harness.product(require_id(line.product_id)).canonical_name == "Kirkland Organic Eggs 24"


def test_a_candidate_within_auto_link_wins_without_asking() -> None:
    harness = _harness(
        one_line_extraction(KIRKLAND_EGGS, store_name=WALMART),
        table={KIRKLAND_EGGS_ALIAS: basis(0)},
    )
    close = _seed(harness, "Close", at_distance(0.1))
    _in_band(harness)

    receipt_id = _walmart_eggs(harness)

    assert harness.line(receipt_id, 0).product_id == close
    assert _matching_requests(harness) == []


def test_thresholds_come_from_settings() -> None:
    """MATCHING__AUTO_LINK_MAX_DISTANCE = 0.3: the 0.25 candidate links without a question."""
    harness = build_harness(
        ScriptedLlm(one_line_extraction(KIRKLAND_EGGS, store_name=WALMART)),
        settings=agent_settings(auto_link_max_distance=0.3, review_max_distance=0.4),
        embeddings=FakeEmbeddings({KIRKLAND_EGGS_ALIAS: basis(0)}),
    )
    candidate = _seed(harness, "Candidate", at_distance(0.25))

    receipt_id = _walmart_eggs(harness)

    assert harness.line(receipt_id, 0).product_id == candidate
    assert harness.line(receipt_id, 0).match_source is VECTOR


# --- S4-AC-07 and the lines that are never matched ----------------------------------------------


def test_s4_ac_07_every_linked_line_has_its_source_and_others_are_never_matched() -> None:
    harness = _harness(mixed_lines_extraction())

    receipt_id = _completed(harness.process_text("mixed"))

    items = harness.receipt(receipt_id).items
    assert [(item.product_id is not None, item.match_source) for item in items] == [
        (True, NEW),
        (False, None),  # DEPOSIT
        (False, None),  # DISCOUNT
        (False, None),  # "PRODUCE": a generic alias
    ]
    assert harness.embeddings.texts == [EGGS_ALIAS, EGGS_ALIAS]  # search, then the new vector
    tools = [name for kind, name in harness.steps(receipt_id) if kind is TOOL]
    assert tools.count("product_lookup") == 1
    assert harness.product_ids() == [require_id(items[0].product_id)]


def test_a_receipt_with_nothing_to_match_still_commits_once() -> None:
    only_deposit: dict[str, Any] = mixed_lines_extraction()
    only_deposit["items"] = only_deposit["items"][1:2]
    only_deposit["total_cents"] = 10
    harness = _harness(only_deposit)

    receipt_id = _completed(harness.process_text("deposit only"))

    assert harness.steps(receipt_id)[-1] == (TOOL, "save_matches")
    event = _save_matches_event(harness, receipt_id)
    assert _traced_sources(event) == {}
    assert harness.product_ids() == []


# --- AC-C-02: the matching agent only reads -----------------------------------------------------


def test_ac_c_02_the_matching_agent_has_read_tools_only() -> None:
    harness = _harness(extraction())

    receipt_id = _costco(harness)

    assert harness.registry.for_role(AgentRole.MATCHING).names == (
        "product_lookup",
        "product_similarity_search",
    )
    events = harness.events(receipt_id)
    matching_tools = {e.name for e in events if e.kind is TOOL and e.agent is AgentRole.MATCHING}
    assert matching_tools == {"product_lookup", "product_similarity_search"}
    (commit,) = [e for e in events if e.kind is TOOL and e.name == "save_matches"]
    assert commit.agent is AgentRole.SUPERVISOR
    nodes = {(e.name, e.agent) for e in events if e.kind is TraceKind.NODE}
    assert ("match", AgentRole.MATCHING) in nodes
    assert ("commit_matches", AgentRole.SUPERVISOR) in nodes


# --- AC-C-03: the memory demo path (experiment E2) ----------------------------------------------


def test_ac_c_03_new_then_alias_then_memory_after_a_correction() -> None:
    """Receipt A creates products; receipt B (same store, same text) hits the aliases with no
    embedding and no matching LLM call; after `items correct`, reprocessing B finds the
    corrected product through correction memory, and the trace says so per line."""
    harness = _harness(extraction(), extraction(), extraction())
    first = _costco(harness, "a.txt")
    second = _completed(harness.process_image("b"))  # FakeOcr reads the same Costco text
    assert harness.match_sources(second) == [ALIAS, ALIAS, ALIAS]
    assert harness.embeddings.calls == 4  # all from receipt A
    old_eggs = harness.line(first, 0).product_id

    corrected = harness.correct(second, 0, new_product_name="Kirkland Organic Eggs 24")
    events_before = len(harness.events(second))
    embeddings_before = harness.embeddings.calls
    again = harness.process_image("b", reprocess=True)

    assert _completed(again) == second
    assert again.duplicate is False
    items = harness.receipt(second).items
    assert (items[0].product_id, items[0].match_source) == (
        corrected.product_id,
        MatchSource.MEMORY,
    )
    assert corrected.product_id != old_eggs
    assert harness.match_sources(second)[1:] == [ALIAS, ALIAS]
    assert harness.embeddings.calls == embeddings_before
    rerun = harness.events(second)[events_before:]
    assert not any(e.kind is TOOL and e.name == "product_similarity_search" for e in rerun)
    event = _save_matches_event(harness, second)
    assert _traced_sources(event) == {0: "memory", 1: "alias", 2: "alias"}
    assert "memory 1" in event.summary
    assert "alias 2" in event.summary


# --- slice C review notes 2 and 3 (pinned in docs/tests/agent-core-D.md) ----------------------


def test_review_note_2_a_line_a_person_linked_in_the_review_is_not_matched_again() -> None:
    """A review answer names the eggs' product (``DraftPayload.product_names``) and ``persist``
    links the line to it. ``match`` must leave that line alone: correction memory would point
    it elsewhere, and a person's answer outranks memory, aliases and vectors. The line gets no
    tool call and no decision; the other lines are matched as usual."""
    harness = build_harness(
        ScriptedLlm(extraction(), inconsistent_extraction()),
        settings=agent_settings(max_text_attempts=1),
    )
    first = _costco(harness, "a.txt")
    remembered = harness.correct(first, 0, new_product_name="Corrected Eggs")
    parked = harness.process_text(COSTCO_OCR, "b.txt")
    assert parked.graph is not None
    assert parked.graph.status is ReceiptStatus.NEEDS_REVIEW
    second = parked.receipt_id
    answer = receipt_draft(store_name=STORE_NAME, product_names={0: "Person's Eggs"})

    harness.resolve(require_id(parked.graph.review_id), answer)

    eggs, milk = harness.receipt(second).items
    linked = harness.product(require_id(eggs.product_id))
    assert linked.canonical_name == "Person's Eggs"
    assert eggs.product_id != remembered.product_id
    assert eggs.match_source is not MatchSource.MEMORY
    assert milk.match_source is ALIAS
    lookups = [e for e in harness.events(second) if e.kind is TOOL and e.name == "product_lookup"]
    assert len(lookups) == 1  # the milk only
    assert _traced_sources(_save_matches_event(harness, second)) == {1: "alias"}


def test_review_note_3_an_answer_about_another_line_is_not_a_confirmation() -> None:
    """The model answers "same" with an offered id and full confidence, but for line 5: the
    receipt line asked about is 0, so the answer does not count and the line is new."""
    harness = _band_harness(match_decision("same", 2, confidence=1.0, line_position=5))
    _in_band(harness)

    walmart = _walmart_eggs(harness)

    (line,) = harness.receipt(walmart).items
    assert line.match_source is NEW
    assert line.product_id not in {1, 2, 3, 4}
    assert len(_matching_requests(harness)) == 1  # asked once, not again
