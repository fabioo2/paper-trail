"""The extraction node with printed-value answers: conversion, parse errors and the leak guard
(EA-AC-05, EA-AC-07, EA-AC-11; docs/plans/extraction-accuracy.md 2.1 and 2.4).

Scripted LLM, in-process tools, fakes; ``extraction/v2`` unless a test says otherwise.

Pinned:
- The node validates the answer with ``ANSWER_MODELS[<prompt id>]`` and calls ``to_output()``;
  the state keeps ``ExtractionOutput`` JSON, so validation, review, persist and matching see
  integer cents. A ``PrintedValueError`` makes the attempt unparsed: ``extraction=None``, its
  message the ``parse_error``, which the next attempt receives in ``<validation_feedback>``.
- ``RenderedPrompt.example_texts() -> frozenset[str]``: the store names and line raw texts of
  the prompt's example outputs (v1 and v2 shapes); empty without examples.
- Leak guard: an answer whose store name, or two or more of whose line raw texts, equal (after
  ``normalize_alias``) ones of ``example_texts()`` is unparsed with the error "the answer copied
  the example receipt; ...". One matching line is not a copy: real receipts print "BANANAS"
  and "LIMES" too (fix round, review finding 2). A generic alias (``is_generic_alias``:
  "instant savings", "deposit" ...) is never a copy: such lines are on real receipts too. With
  ``--no-examples`` nothing is guarded.
- An extraction prompt version without an answer model fails when the graph is built
  (``LookupError``).
"""

import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.agents.graph import build_receipt_graph
from paper_trail.agents.prompts import PROMPTS_DIR, PromptLibrary
from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import ReceiptStatus, ReviewReason
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import (
    STORE_NAME,
    TOTAL_CENTS,
    extraction,
    mixed_lines_extraction,
    printed_extraction,
    printed_fixture,
)
from tests.fakes.llm import Reply, ScriptedLlm

COPIED = "copied the example"
FEEDBACK_TAG = "<validation_feedback>"


def _harness(*replies: Reply, examples: bool = True, version: str = "v2") -> Harness:
    settings = agent_settings(extraction_version=version, examples=examples)
    return build_harness(ScriptedLlm(*replies), settings=settings)


def _graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


def _v2_example_outputs() -> list[dict[str, Any]]:
    prompt = PromptLibrary().load("extraction", "v2")
    assert prompt.examples, "extraction/v2 has examples"
    return [dict(output) for _, output in prompt.examples]


# --- conversion in the node ----------------------------------------------------------------------


def test_a_printed_answer_is_stored_as_integer_cents() -> None:
    harness = _harness(printed_extraction())

    graph = _graph(harness.process_image())

    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.llm.requests[0].tag == "extraction/v2"
    receipt = harness.receipt(graph.receipt_id)
    assert receipt.total is not None
    assert receipt.total.amount_cents == TOTAL_CENTS
    assert [line.line_total.amount_cents for line in receipt.items] == [899, 658, 998]


def test_ea_ac_04_the_costco_answer_completes_end_to_end() -> None:
    """The success criterion of the plan's experiment, on fakes: the Costco photo's printed
    answer is saved COMPLETED with all 22 lines and its date."""
    harness = _harness(printed_fixture("costco_printed"))

    graph = _graph(harness.process_image())

    assert graph.status is ReceiptStatus.COMPLETED
    receipt = harness.receipt(graph.receipt_id)
    assert len(receipt.items) == 22
    assert receipt.total is not None
    assert receipt.total.amount_cents == 20319
    assert receipt.purchased_on == date(2026, 11, 5)


def test_ea_ac_05_a_printed_value_error_is_the_next_attempts_feedback() -> None:
    bad = printed_extraction(total="2683")
    bad["items"][2]["amount"] = "1290"
    harness = _harness(bad, printed_extraction())

    graph = _graph(harness.process_image())

    assert harness.llm.calls == 2
    retry = harness.llm.last_user_message(1)
    assert FEEDBACK_TAG in retry
    for quoted in ('"1290"', '"2683"', "line 3", "total"):
        assert quoted in retry
    assert graph.status is ReceiptStatus.COMPLETED
    assert graph.text_attempts == 2
    first_route = next(
        event
        for event in harness.events(graph.receipt_id)
        if event.kind is TraceKind.ROUTE and event.name == "extract_text"
    )
    feedback = first_route.data["feedback"]
    assert isinstance(feedback, list)
    assert len(feedback) == 1
    assert '"1290"' in str(feedback[0])


def test_ea_ac_05_an_answer_that_never_converts_ends_in_extraction_failed() -> None:
    bad = printed_extraction()
    bad["items"][0]["amount"] = "899"
    harness = _harness(bad, bad)

    graph = _graph(harness.process_image())

    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.EXTRACTION_FAILED


# --- example_texts --------------------------------------------------------------------------------


def test_example_texts_cover_the_v1_examples() -> None:
    texts = {
        normalize_alias(text) for text in PromptLibrary().load("extraction", "v1").example_texts()
    }

    assert {"walmart supercentre #3114", "gv peanut butter 1kg", "no frills #3021"} <= texts


def test_example_texts_cover_every_v2_store_and_line() -> None:
    texts = {
        normalize_alias(text) for text in PromptLibrary().load("extraction", "v2").example_texts()
    }

    for output in _v2_example_outputs():
        assert normalize_alias(output["store_name"]) in texts
        for line in output["items"]:
            assert normalize_alias(line["raw_text"]) in texts


@pytest.mark.parametrize(("name", "version"), [("extraction", "v1"), ("extraction", "v2")])
def test_without_examples_there_is_nothing_to_copy(name: str, version: str) -> None:
    assert PromptLibrary().load(name, version, examples=False).example_texts() == frozenset()


# --- EA-AC-07: the leak guard ---------------------------------------------------------------------


def _copied_store(store_name: str) -> dict[str, Any]:
    return printed_extraction(store_name=store_name)


def _copied_line(raw_text: str) -> dict[str, Any]:
    answer = printed_extraction()
    answer["items"][0]["raw_text"] = raw_text
    return answer


def test_ea_ac_07_an_answer_with_an_examples_store_is_unparsed() -> None:
    example_store = _v2_example_outputs()[0]["store_name"]
    harness = _harness(_copied_store(example_store), printed_extraction())

    graph = _graph(harness.process_image())

    assert harness.llm.calls == 2
    assert COPIED in harness.llm.last_user_message(1)
    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.receipt(graph.receipt_id).store_name == STORE_NAME


def test_ea_ac_07_spelling_does_not_hide_a_copy() -> None:
    example_store = _v2_example_outputs()[0]["store_name"]
    harness = _harness(_copied_store(f"  {example_store.lower()} "), printed_extraction())

    harness.process_image()

    assert COPIED in harness.llm.last_user_message(1)


def _example_item_lines() -> list[str]:
    return [
        line["raw_text"]
        for output in _v2_example_outputs()
        for line in output["items"]
        if line["kind"] == "item"
    ]


def test_ea_ac_07_an_answer_with_two_examples_lines_is_unparsed() -> None:
    first, second = _example_item_lines()[:2]
    copied = _copied_line(first)
    copied["items"][1]["raw_text"] = second
    harness = _harness(copied, printed_extraction())

    harness.process_image()

    assert harness.llm.calls == 2
    assert COPIED in harness.llm.last_user_message(1)


def test_ea_ac_07_one_line_like_an_examples_is_not_a_copy() -> None:
    harness = _harness(_copied_line(_example_item_lines()[0]))

    graph = _graph(harness.process_image())

    assert harness.llm.calls == 1
    assert graph.status is ReceiptStatus.COMPLETED


def test_ea_ac_07_a_real_line_also_in_a_v1_example_is_not_a_copy() -> None:
    """The --prompt-version v1 control run: v1 examples print real products (BANANAS, LIMES)."""
    answer = extraction()
    answer["items"][0]["raw_text"] = "BANANAS"
    answer["items"][1]["raw_text"] = "KS 2% MILK 4L"
    harness = _harness(answer, version="v1")

    graph = _graph(harness.process_image())

    assert harness.llm.calls == 1
    assert graph.status is ReceiptStatus.COMPLETED


def test_ea_ac_07_v1_examples_are_guarded_too() -> None:
    """The real leak (evidence 2): example 05's WALMART receipt came back for a Costco photo."""
    copied = extraction(store_name="WALMART SUPERCENTRE #3114")
    harness = _harness(copied, extraction(), version="v1")

    graph = _graph(harness.process_image())

    assert harness.llm.calls == 2
    assert COPIED in harness.llm.last_user_message(1)
    assert graph.status is ReceiptStatus.COMPLETED


def test_ea_ac_07_a_generic_line_like_an_examples_is_not_a_copy() -> None:
    """v1 example 03 has an "INSTANT SAVINGS" line; so do real receipts."""
    harness = _harness(mixed_lines_extraction(), version="v1")

    graph = _graph(harness.process_text("mixed"))

    assert harness.llm.calls == 1
    assert graph.status is ReceiptStatus.COMPLETED


def test_ea_ac_07_without_examples_nothing_is_guarded() -> None:
    example_store = _v2_example_outputs()[0]["store_name"]
    harness = _harness(_copied_store(example_store), examples=False)

    graph = _graph(harness.process_image())

    assert harness.llm.requests[0].tag == "extraction/v2+noex"
    assert harness.llm.calls == 1
    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.receipt(graph.receipt_id).store_name == example_store


# --- answer models --------------------------------------------------------------------------------


def test_an_extraction_version_without_an_answer_model_fails_at_graph_build(
    tmp_path: Path,
) -> None:
    """A prompt directory alone is not enough: the node must know the answer's shape."""
    root = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, root)
    shutil.copytree(root / "extraction" / "v1", root / "extraction" / "v7")
    harness = build_harness(ScriptedLlm())
    deps = replace(
        harness.deps,
        prompts=PromptLibrary(root),
        settings=agent_settings(extraction_version="v7"),
    )

    with pytest.raises(LookupError):
        build_receipt_graph(deps, InMemorySaver())
