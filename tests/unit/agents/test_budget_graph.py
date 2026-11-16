"""The monthly Claude budget through the graph (claude-extraction plan R2, E7; CE-TC-038, 039).

The harness's reader is ``BudgetedReader(ScriptedReader, FakeSpendLedger, clock, cap)``, as the
composition root wraps the Anthropic adapter.

Pinned:
- at the cap no read is made and the receipt is FAILED with "monthly Claude budget reached
  (spent X of Y USD)" and its checkpoint, resumable (one wiring case: the admission rules are
  ``tests/unit/application/test_budget.py``'s);
- a higher cap, or the first of the next month, lets ``resume`` finish the receipt.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from paper_trail.application.budget import BudgetedReader
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.ports.budget import ClaudeCall
from paper_trail.ports.reader import BudgetReachedError, ReaderError
from tests.agent_harness import EXTRACT_MODEL, RETRY_MODEL, Harness, build_harness
from tests.extractions import NOW, answer, inconsistent_answer
from tests.fakes.budget import FakeSpendLedger
from tests.fakes.clock import FakeClock
from tests.fakes.reader import Reply, ScriptedReader
from tests.unit.agents.graph_support import graph, saved_values

CAP = Decimal("5.00")
NEXT_MONTH = datetime(2026, 12, 1, tzinfo=UTC)


def _spent(amount: str, at: datetime = NOW) -> ClaudeCall:
    return ClaudeCall(
        at=at, model=EXTRACT_MODEL, input_tokens=0, output_tokens=0, cost_usd=Decimal(amount)
    )


def _harness(
    ledger: FakeSpendLedger, *replies: Reply, cap: Decimal = CAP
) -> tuple[Harness, ScriptedReader]:
    clock = FakeClock(NOW)
    reader = ScriptedReader(*replies)
    return build_harness(BudgetedReader(reader, ledger, clock, cap), clock=clock), reader


def _resumed_with(
    first: Harness, ledger: FakeSpendLedger, cap: Decimal, *replies: Reply
) -> tuple[Harness, ScriptedReader]:
    reader = ScriptedReader(*replies)
    budgeted = BudgetedReader(reader, ledger, first.clock, cap)
    return (
        build_harness(
            budgeted,
            settings=first.settings,
            clock=first.clock,
            checkpointer=first.checkpointer,
            uow_factory=first.uow_factory,
            images=first.images,
        ),
        reader,
    )


def _assert_parked(harness: Harness, receipt_id: int) -> None:
    stored = harness.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    saved_values(harness.checkpointer, receipt_id)


def test_ce_tc_038_at_the_cap_no_read_is_made_and_the_receipt_is_parked() -> None:
    """Wiring: the admission rules are owned by ``tests/unit/application/test_budget.py``."""
    ledger = FakeSpendLedger(_spent("5.00"))
    harness, reader = _harness(ledger, answer())

    with pytest.raises(ReaderError) as stopped:
        harness.process_image()

    assert isinstance(stopped.value.__cause__, BudgetReachedError)
    assert str(stopped.value).startswith("monthly Claude budget reached (spent 5.00 of 5.00 USD)")
    assert reader.calls == 0
    _assert_parked(harness, 1)


def test_ce_tc_039_a_higher_cap_lets_resume_finish() -> None:
    ledger = FakeSpendLedger(_spent("4.9999"))
    first, _ = _harness(ledger, inconsistent_answer())
    with pytest.raises(ReaderError):
        first.process_image()
    later, reader = _resumed_with(first, ledger, Decimal("10.00"), answer())

    resumed = graph(later.resume(1))

    assert (resumed.status, reader.models) == (ReceiptStatus.COMPLETED, [RETRY_MODEL])
    assert later.receipt(1).status is ReceiptStatus.COMPLETED


def test_ce_tc_039_a_new_month_lets_resume_finish_under_the_same_cap() -> None:
    ledger = FakeSpendLedger(_spent("5.00"))
    first, _ = _harness(ledger, answer())
    with pytest.raises(ReaderError):
        first.process_image()
    first.clock.set(NEXT_MONTH)
    later, reader = _resumed_with(first, ledger, CAP, answer())

    resumed = graph(later.resume(1))

    assert (resumed.status, reader.models) == (ReceiptStatus.COMPLETED, [EXTRACT_MODEL])
