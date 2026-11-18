"""The monthly Claude budget around the reader (claude-extraction plan R2, E7; CE-TC-037, 038).

Pinned: before each read the spend since the start of the UTC month is compared with the cap;
at or over it, ``BudgetReachedError`` ("monthly Claude budget reached (spent X of Y USD)") and
no call. Under it the call is made even if it overshoots. After each billed read, a failed one
included, one ``ClaudeCall`` (now, model, tokens, ``call_cost_usd``) is recorded; a failure
with no usage records nothing and propagates unchanged.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.application.budget import BudgetedReader
from paper_trail.domain.errors import UnsupportedMediaError
from paper_trail.ports.budget import ClaudeCall
from paper_trail.ports.reader import (
    BudgetReachedError,
    ReaderOutputError,
    ReaderUnavailableError,
    ReadRequest,
    ReadUsage,
)
from tests.extractions import answer
from tests.fakes.budget import FakeSpendLedger
from tests.fakes.clock import FakeClock
from tests.fakes.reader import INPUT_TOKENS, OUTPUT_TOKENS, ScriptedReader

NOW = datetime(2026, 11, 15, 19, 0, tzinfo=UTC)
HAIKU = "claude-haiku-5-5"
CAP = Decimal("5.00")
# INPUT_TOKENS x 0.10 + OUTPUT_TOKENS x 0.50 per million.
HAIKU_READ_COST = Decimal("0.0005")


def _request(model: str = HAIKU) -> ReadRequest:
    return ReadRequest(
        model=model, tag="extraction/test", system="", text="", image=b"photo", max_tokens=1000
    )


def _spent(cost: str, at: datetime = NOW) -> ClaudeCall:
    return ClaudeCall(at=at, model=HAIKU, input_tokens=0, output_tokens=0, cost_usd=Decimal(cost))


def _budgeted(inner: ScriptedReader, ledger: FakeSpendLedger, cap: Decimal = CAP) -> BudgetedReader:
    return BudgetedReader(inner, ledger, FakeClock(NOW), cap)


def test_ce_tc_038_a_read_under_the_cap_is_made_and_recorded() -> None:
    inner = ScriptedReader(answer())
    ledger = FakeSpendLedger()

    result = _budgeted(inner, ledger).read(_request(), ReceiptAnswer)

    assert (inner.calls, result.usage) == (1, ReadUsage(INPUT_TOKENS, OUTPUT_TOKENS))
    assert (ledger.calls, ledger.reservations) == (
        [ClaudeCall(NOW, HAIKU, INPUT_TOKENS, OUTPUT_TOKENS, HAIKU_READ_COST)],
        {},
    )


@pytest.mark.parametrize(
    ("spent", "cap", "message"),
    [
        ("5.00", CAP, "monthly Claude budget reached (spent 5.00 of 5.00 USD)"),
        ("7.50", CAP, "monthly Claude budget reached (spent 7.50 of 5.00 USD)"),
        ("0.00", Decimal("0.00"), "monthly Claude budget reached (spent 0.00 of 0.00 USD)"),
    ],
    ids=["at-cap", "over-cap", "cap-zero"],
)
def test_ce_tc_038_at_the_cap_no_call_is_made(spent: str, cap: Decimal, message: str) -> None:
    inner = ScriptedReader()
    ledger = FakeSpendLedger(_spent(spent))

    with pytest.raises(BudgetReachedError) as raised:
        _budgeted(inner, ledger, cap).read(_request(), ReceiptAnswer)

    assert (str(raised.value).startswith(message), inner.calls, len(ledger.calls)) == (
        True,
        0,
        1,
    )


def test_ce_tc_038_a_call_that_starts_under_the_cap_may_overshoot_it() -> None:
    inner = ScriptedReader(answer())
    ledger = FakeSpendLedger(_spent("4.9999"))

    _budgeted(inner, ledger).read(_request(), ReceiptAnswer)

    assert (inner.calls, ledger.spent_since(NOW)) == (1, Decimal("5.0004"))


def test_ce_tc_039_last_months_spend_does_not_count() -> None:
    inner = ScriptedReader(answer())
    ledger = FakeSpendLedger(_spent("10.00", datetime(2026, 10, 31, 23, 59, tzinfo=UTC)))

    _budgeted(inner, ledger).read(_request(), ReceiptAnswer)

    assert inner.calls == 1


def _billed_output_error() -> ReaderOutputError:
    error = ReaderOutputError("items.0.price_paid: Input should be a valid string")
    error.usage = ReadUsage(input_tokens=1000, output_tokens=200)
    return error


@pytest.mark.parametrize(
    ("error", "recorded"),
    [
        (_billed_output_error(), [ClaudeCall(NOW, HAIKU, 1000, 200, Decimal("0.0002"))]),
        (ReaderUnavailableError("the Claude API is unreachable"), []),
        (UnsupportedMediaError("the stored photo cannot be read"), []),
    ],
    ids=["billed-failure", "unbilled-failure", "undecodable-photo"],
)
def test_ce_tc_037_a_failed_read_is_recorded_only_when_billed(
    error: Exception, recorded: list[ClaudeCall]
) -> None:
    ledger = FakeSpendLedger()

    with pytest.raises(type(error)) as raised:
        _budgeted(ScriptedReader(error), ledger).read(_request(), ReceiptAnswer)

    assert (raised.value, ledger.calls, ledger.reservations) == (error, recorded, {})


def test_h4_12_reservations_left_open_by_killed_calls_fill_the_budget() -> None:
    inner = ScriptedReader(answer())
    ledger = FakeSpendLedger()
    ledger.reservations = {1: (datetime(2026, 11, 1, tzinfo=UTC), CAP)}

    with pytest.raises(BudgetReachedError, match=r"spent 5\.00 of 5\.00 USD"):
        _budgeted(inner, ledger).read(_request(), ReceiptAnswer)

    assert inner.calls == 0
