"""The tool transport changes nothing a run does (S5-AC-04, AC-B-03, agent core slice B).

The graph suites run their tools in-process. Here a representative set of scenarios runs twice,
once with ``InProcessToolGateway`` and once with ``McpToolGateway`` talking to the production
MCP app on a loopback port (``tests.agent_harness.tool_transport``), and everything observable
about the run must be equal: the outcomes, every trace event (minus timing), the stored
receipt, the reviews, Claude's reads and the ``tool.denied`` log lines (CE-TC-033: the Metro
answer is one of the scenarios).

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
from paper_trail.domain.enums import (
    AgentRole,
    CodeKind,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.models import JsonValue, Receipt, ReviewItem
from paper_trail.domain.rules.item_codes import ItemCode
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.catalog import CatalogProduct
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import (
    TOOL_TRANSPORTS,
    Harness,
    agent_settings,
    build_harness,
    tool_transport,
)
from tests.extractions import C1, L3, L4, answer, coded_answer, inconsistent_answer, metro_answer
from tests.factories import receipt_draft
from tests.fakes.catalog import ORGANIC_TEST_APPLE, TEST_APPLE
from tests.fakes.reader import ScriptedReader
from tests.fakes.tools import MisroutedToolGateway
from tests.support import require_id
from tests.unit.agents.graph_support import scripted

type Event = tuple[int, AgentRole, TraceKind, str, str, dict[str, JsonValue]]


@dataclass(frozen=True)
class Observed:
    """Everything a scenario's runs left behind, comparable across transports."""

    outcomes: tuple[ProcessOutcome | Receipt, ...]
    events: tuple[Event, ...]
    receipt: Receipt
    reviews: tuple[ReviewItem, ...]
    reads: int
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
        reads=sum(scripted(harness).calls for harness in harnesses),
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
    observed = _single_run(build_harness(ScriptedReader(answer())))
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.tools[-1] == "save_matches"
    return observed


def validation_retry() -> Observed:
    """The first answer does not add up; the retry with the issues does."""
    harness = build_harness(ScriptedReader(inconsistent_answer(), answer()))
    observed = _single_run(harness)
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.reads == 2
    return observed


def review_routing() -> Observed:
    """Never adds up: the supervisor opens a review and the graph waits for a person."""
    observed = _single_run(build_harness(ScriptedReader(then=inconsistent_answer())))
    assert observed.receipt.status is ReceiptStatus.NEEDS_REVIEW
    assert [(r.reason, r.status) for r in observed.reviews] == [
        (ReviewReason.VALIDATION_FAILED, ReviewStatus.OPEN)
    ]
    return observed


def denied_tool() -> Observed:
    """A node calls ``save_receipt`` as the matching agent: the registry (over MCP, the
    server) denies it, and the run fails loudly."""
    harness = build_harness(
        ScriptedReader(answer()),
        wrap_tools=lambda gateway: MisroutedToolGateway(
            gateway, "save_receipt", AgentRole.MATCHING
        ),
    )
    observed = _single_run(harness)
    assert observed.receipt.status is ReceiptStatus.FAILED
    assert observed.denied == (("matching", "save_receipt"),)
    return observed


def resume() -> Observed:
    """``--interrupt-after extract`` in one process, ``--resume`` in a second one."""
    first = build_harness(ScriptedReader(answer()))
    with capture_logs() as logs:
        paused = first.process_image(interrupt_after="extract")
        second = build_harness(
            ScriptedReader(),
            settings=first.settings,
            checkpointer=first.checkpointer,
            uow_factory=first.uow_factory,
            images=first.images,
        )
        resumed = second.resume(paused.receipt_id)
    observed = _observe((first, second), paused.receipt_id, (paused, resumed), logs)
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.reads == 1, "the resumed run does not read again"
    return observed


def review_resolution() -> Observed:
    """A parked receipt resolved with the person's draft: the graph resumes and saves it."""
    harness = build_harness(
        ScriptedReader(inconsistent_answer()), settings=agent_settings(max_attempts=1)
    )
    with capture_logs() as logs:
        parked = harness.process_image()
        assert parked.graph is not None
        resolved = harness.resolve(
            require_id(parked.graph.review_id),
            receipt_draft(store_name="Costco", ocr_text=None, image_path=None),
            force=True,
            reason="checked the paper",
        )
    observed = _observe((harness,), parked.receipt_id, (parked, resolved), logs)
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert [review.status for review in observed.reviews] == [ReviewStatus.RESOLVED]
    return observed


class _PluTable:
    """The PLU table of the barcode-lookup cases: 3101 "Test Apple", its organic form named
    "Organic Test Apple" (a ``ProductCatalog``)."""

    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None:
        if code.kind is not CodeKind.PLU or code.keys != ("3101",):
            return None
        return ORGANIC_TEST_APPLE if code.organic else TEST_APPLE


def catalog() -> Observed:
    """Barcode lookup PC-TC-62: the coded items are matched by code and the PLU-named products
    are described with ``describe_from_catalog``; no read after the extraction."""
    settings = agent_settings(enrichment=True)
    harness = build_harness(
        ScriptedReader(coded_answer([C1, L3, L4])), settings=settings, catalog=_PluTable()
    )
    observed = _single_run(harness)
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    assert observed.reads == 1
    assert observed.tools[-9:] == [
        *["catalog_lookup", "product_lookup"] * 3,
        "save_matches",
        "describe_from_catalog",
        "describe_from_catalog",
    ]
    return observed


def metro() -> Observed:
    """CE-TC-033: the Metro answer; ``save_receipt`` keeps its five item discounts linked; the
    links and the tool's result are part of what is compared."""
    observed = _single_run(build_harness(ScriptedReader(metro_answer())))
    assert observed.receipt.status is ReceiptStatus.COMPLETED
    links = {
        item.position: item.applies_to_position
        for item in observed.receipt.items
        if item.applies_to_position is not None
    }
    assert links == {3: 2, 5: 4, 7: 6, 9: 8, 11: 10}
    (saved,) = [
        data["output"]
        for _, _, kind, name, _, data in observed.events
        if kind is TraceKind.TOOL and name == "save_receipt"
    ]
    assert isinstance(saved, dict)
    refused = saved["discounts_receipt_level"]
    assert isinstance(refused, list)
    assert (saved["discounts_linked"], len(refused)) == (5, 0)
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
        catalog,
        metro,
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
        assert isinstance(build_harness(ScriptedReader()).deps.tools, McpToolGateway)
    assert isinstance(build_harness(ScriptedReader()).deps.tools, InProcessToolGateway)
