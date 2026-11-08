"""The tool transport changes nothing a run does (S5-AC-04, AC-B-03, agent core slice B).

The graph suites run their tools in-process. Here a representative set of scenarios runs twice,
once with ``InProcessToolGateway`` and once with ``McpToolGateway`` talking to the production
MCP app on a loopback port (``tests.agent_harness.tool_transport``), and everything observable
about the run must be equal: the outcomes, every trace event (minus timing), the stored
receipt, the reviews, the model calls and the ``tool.denied`` log lines.

Each scenario also asserts what it is about, so two runs that broke the same way cannot pass as
equal. A change to how tools are called gets a scenario here; the in-process suites stay the
place for the behaviour itself.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.adapters.mcp.gateway import McpToolGateway
from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import AgentRole, ReceiptStatus, ReviewReason, ReviewStatus
from paper_trail.domain.models import JsonValue, Receipt, ReviewItem
from paper_trail.domain.trace import TraceKind
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import (
    TOOL_TRANSPORTS,
    Harness,
    agent_settings,
    build_harness,
    tool_transport,
)
from tests.extractions import (
    COSTCO_ENRICHMENTS,
    COSTCO_OCR,
    extraction,
    inconsistent_extraction,
    printed_extraction,
)
from tests.factories import receipt_draft
from tests.fakes.llm import ScriptedLlm
from tests.fakes.tools import MisroutedToolGateway
from tests.support import require_id

type Event = tuple[int, AgentRole, TraceKind, str, str, dict[str, JsonValue]]


@dataclass(frozen=True)
class Observed:
    """Everything a scenario's runs left behind, comparable across transports."""

    outcomes: tuple[ProcessOutcome | Receipt, ...]
    events: tuple[Event, ...]
    receipt: Receipt
    reviews: tuple[ReviewItem, ...]
    llm_calls: int
    denied: tuple[tuple[str, str], ...]

    @property
    def tools(self) -> list[str]:
        return [name for _, _, kind, name, _, _ in self.events if kind is TraceKind.TOOL]


def _observe(
    harnesses: tuple[Harness, ...],
    receipt_id: int,
    outcomes: tuple[ProcessOutcome | Receipt, ...],
    logs: Sequence[Mapping[str, Any]],
) -> Observed:
    last = harnesses[-1]
    reviews: list[ReviewItem] = []  # every review, open or not; ids count up from 1
    with last.uow_factory(read_only=True) as uow:
        while (review := uow.reviews.get(len(reviews) + 1)) is not None:
            reviews.append(review)
    return Observed(
        outcomes=outcomes,
        events=tuple(
            (event.seq, event.agent, event.kind, event.name, event.summary, dict(event.data))
            for harness in harnesses
            for event in harness.events(receipt_id)
        ),
        receipt=last.receipt(receipt_id),
        reviews=tuple(reviews),
        llm_calls=sum(harness.llm.calls for harness in harnesses),
        denied=tuple(
            (str(entry["role"]), str(entry["tool"]))
            for entry in logs
            if entry["event"] == "tool.denied"
        ),
    )


def _single_run(harness: Harness) -> Observed:
    with capture_logs() as logs:
        outcome = harness.process_image()
    return _observe((harness,), outcome.receipt_id, (outcome,), logs)


# --- the scenarios ------------------------------------------------------------------------------


def clean_run() -> Observed:
    """Extract, validate, save, then match every line (slice C's matching tools)."""
    observed = _single_run(build_harness(ScriptedLlm(extraction())))
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.tools[-1] == "save_matches"
    return observed


def validation_retry() -> Observed:
    """The first answer does not add up; the retry with the issues does."""
    observed = _single_run(build_harness(ScriptedLlm(inconsistent_extraction(), extraction())))
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.tools.count("check_arithmetic") == 2
    return observed


def review_routing() -> Observed:
    """Never adds up: the supervisor opens a review and the graph waits for a person."""
    llm = ScriptedLlm(inconsistent_extraction(), then=inconsistent_extraction(line=1))
    observed = _single_run(build_harness(llm, settings=agent_settings(max_vision_attempts=0)))
    assert observed.receipt.status is ReceiptStatus.NEEDS_REVIEW
    assert [(r.reason, r.status) for r in observed.reviews] == [
        (ReviewReason.VALIDATION_FAILED, ReviewStatus.OPEN)
    ]
    return observed


def denied_tool() -> Observed:
    """A node calls ``save_receipt`` as the matching agent: the registry (over MCP, the
    server) denies it, and the run fails loudly."""
    harness = build_harness(
        ScriptedLlm(extraction()),
        wrap_tools=lambda gateway: MisroutedToolGateway(
            gateway, "save_receipt", AgentRole.MATCHING
        ),
    )
    observed = _single_run(harness)
    assert observed.receipt.status is ReceiptStatus.FAILED
    assert observed.denied == (("matching", "save_receipt"),)
    return observed


def resume() -> Observed:
    """``--interrupt-after extract_text`` in one process, ``--resume`` in a second one."""
    first = build_harness(ScriptedLlm(extraction()))
    with capture_logs() as logs:
        paused = first.process_text(COSTCO_OCR, interrupt_after="extract_text")
        second = build_harness(
            ScriptedLlm(),
            settings=first.settings,
            checkpointer=first.checkpointer,
            uow_factory=first.uow_factory,
            images=first.images,
        )
        resumed = second.resume(paused.receipt_id)
    observed = _observe((first, second), paused.receipt_id, (paused, resumed), logs)
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.llm_calls == 1, "the resumed run does not extract again"
    return observed


def review_resolution() -> Observed:
    """A parked receipt resolved with the person's draft: the graph resumes and saves it."""
    harness = build_harness(
        ScriptedLlm(inconsistent_extraction()), settings=agent_settings(max_text_attempts=1)
    )
    with capture_logs() as logs:
        parked = harness.process_image()
        assert parked.graph is not None
        resolved = harness.resolve(
            require_id(parked.graph.review_id),
            receipt_draft(store_name="Costco", ocr_text=None, image_path=None),
        )
    observed = _observe((harness,), parked.receipt_id, (parked, resolved), logs)
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert [review.status for review in observed.reviews] == [ReviewStatus.RESOLVED]
    return observed


def vision_and_enrichment() -> Observed:
    """Slice D: both text attempts fail, the vision attempt reads the photo, then the new
    products are enriched and the supervisor saves them with ``save_enrichment``."""
    llm = ScriptedLlm(
        inconsistent_extraction(),
        inconsistent_extraction(),
        printed_extraction(),  # the vision answer: printed values (vision/v1)
        *COSTCO_ENRICHMENTS,
    )
    settings = agent_settings(max_vision_attempts=1, enrichment=True)
    observed = _single_run(build_harness(llm, settings=settings))
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.llm_calls == 6
    assert observed.tools[-4:] == ["save_matches"] + ["save_enrichment"] * 3
    return observed


SCENARIOS: dict[str, Callable[[], Observed]] = {
    scenario.__name__: scenario
    for scenario in (
        clean_run,
        validation_retry,
        review_routing,
        denied_tool,
        resume,
        review_resolution,
        vision_and_enrichment,
    )
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_s5_ac_04_a_run_is_the_same_over_both_transports(scenario: str) -> None:
    observed: dict[str, Observed] = {}
    for transport in TOOL_TRANSPORTS:
        with tool_transport(transport):
            observed[transport] = SCENARIOS[scenario]()

    assert observed["mcp"] == observed["inprocess"]


def test_the_mcp_runs_really_call_their_tools_over_mcp() -> None:
    """Guards the comparison above against comparing in-process with in-process."""
    with tool_transport("mcp"):
        assert isinstance(build_harness(ScriptedLlm()).deps.tools, McpToolGateway)
    assert isinstance(build_harness(ScriptedLlm()).deps.tools, InProcessToolGateway)
