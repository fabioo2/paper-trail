"""The month's Claude spend on real SQLite (claude-extraction plan R2, CQ4; CE-TC-037).

``SqliteSpendLedger`` keeps one ``claude_calls`` row per billed call (a reservation settled
with the call's real cost, review R4; admission is test_budget_admission_sqlite.py), costed with
``call_cost_usd`` from the price table; the month is the UTC calendar month (``month_start``).
A failed call with no usage is never recorded (owned by the ``BudgetedReader`` unit suite).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.spend_ledger import SqliteSpendLedger
from paper_trail.domain.rules.budget import call_cost_usd, month_start
from paper_trail.ports.budget import ClaudeCall

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
LAST_MONTH = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)


def _call(model: str, input_tokens: int, output_tokens: int, at: datetime = NOW) -> ClaudeCall:
    return ClaudeCall(
        at=at,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=call_cost_usd(model, input_tokens, output_tokens),
    )


NO_CAP = Decimal(1_000_000)


def _book(ledger: SqliteSpendLedger, call: ClaudeCall) -> None:
    """A call admitted and then billed: its reservation settled with the real cost."""
    reservation = ledger.reserve(
        call.at, call.model, Decimal(1), since=month_start(call.at), cap_usd=NO_CAP
    )
    assert reservation is not None
    ledger.settle(reservation, call)


@pytest.fixture
def ledger(migrated_engine: Engine) -> SqliteSpendLedger:
    return SqliteSpendLedger(migrated_engine)


def test_ce_tc_037_the_month_spend_is_the_tokens_times_the_price_table(
    ledger: SqliteSpendLedger, migrated_engine: Engine
) -> None:
    """Haiku 5.5: 1,500 in x $0.10 + 700 out x $0.50 per MTok = $0.0005; Opus 5.5: 2,000 in x $4
    + 1,000 out x $20 = $0.028. The October call is not in November's sum."""
    _book(ledger, _call("claude-haiku-5-5", 1500, 700))
    _book(ledger, _call("claude-opus-5-5", 2000, 1000))
    _book(ledger, _call("claude-opus-5-5", 2000, 1000, at=LAST_MONTH))

    spent = ledger.spent_since(month_start(NOW))

    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT model, input_tokens, output_tokens FROM claude_calls ORDER BY id")
        ).all()
    assert spent == Decimal("0.0005") + Decimal("0.028") == Decimal("0.0285")
    assert [tuple(row) for row in rows] == [
        ("claude-haiku-5-5", 1500, 700),
        ("claude-opus-5-5", 2000, 1000),
        ("claude-opus-5-5", 2000, 1000),
    ]


def test_ce_tc_037_a_call_at_the_month_start_counts(ledger: SqliteSpendLedger) -> None:
    start = month_start(NOW)
    _book(ledger, _call("claude-haiku-5-5", 1500, 700, at=start))

    assert (start, ledger.spent_since(start)) == (
        datetime(2026, 11, 1, tzinfo=UTC),
        Decimal("0.0005"),
    )


def test_ce_tc_037_nothing_recorded_is_zero(ledger: SqliteSpendLedger) -> None:
    assert ledger.spent_since(month_start(NOW)) == Decimal(0)


def test_ce_tc_037_a_record_is_committed_on_its_own(
    ledger: SqliteSpendLedger, migrated_engine: Engine
) -> None:
    """Counted even when the receipt's run then fails: another engine sees it at once."""
    _book(ledger, _call("claude-haiku-5-5", 1500, 700))

    with migrated_engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM claude_calls")).scalar_one()
    assert count == 1
