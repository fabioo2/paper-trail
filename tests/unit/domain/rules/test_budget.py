"""What a Claude call costs and when the month's budget stops the next one (claude-extraction
plan R2, E7; ruling I9).

Pinned: prices are USD per million tokens (Haiku 5.5 0.10 / 0.50, Opus 5.5 4 / 20), exact in
``Decimal``; every model the settings accept has a price (ruling C6); the month is the UTC
calendar month;
the budget is reached when the spend is at or above the cap, so a cap of 0 stops every call.
"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from paper_trail.domain.rules.budget import (
    MODEL_PRICES,
    budget_reached,
    call_ceiling_usd,
    call_cost_usd,
    month_start,
)

EASTERN = timezone(timedelta(hours=-5))


@pytest.mark.parametrize(
    ("model", "input_tokens", "output_tokens", "cost"),
    [
        ("claude-haiku-5-5", 1_000_000, 1_000_000, Decimal("0.60")),
        ("claude-haiku-5-5", 1500, 700, Decimal("0.0005")),
        ("claude-opus-5-5", 1500, 700, Decimal("0.020")),
        ("claude-opus-5-5", 0, 0, Decimal(0)),
    ],
    ids=["haiku-mtok", "haiku-receipt", "opus-receipt", "nothing"],
)
def test_ce_tc_037_a_call_costs_its_tokens_at_the_models_price(
    model: str, input_tokens: int, output_tokens: int, cost: Decimal
) -> None:
    assert call_cost_usd(model, input_tokens, output_tokens) == cost


@pytest.mark.parametrize(
    ("now", "start"),
    [
        (datetime(2026, 11, 15, 19, 0, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
        (datetime(2026, 11, 1, 0, 0, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
        (datetime(2026, 11, 30, 22, 0, tzinfo=EASTERN), datetime(2026, 12, 1, tzinfo=UTC)),
        (datetime(2026, 12, 31, 23, 59, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)),
    ],
    ids=["mid-month", "first-instant", "local-evening-is-next-utc-month", "last-minute"],
)
def test_e7_the_budget_month_is_the_utc_calendar_month(now: datetime, start: datetime) -> None:
    assert month_start(now) == start


def test_ce_tc_038_the_budget_is_reached_at_the_cap() -> None:
    """One row: the cases (under, at, over, cap 0) are owned by
    tests/unit/application/test_budget.py."""
    assert budget_reached(Decimal("5.00"), Decimal("5.00")) is True


def test_c6_the_price_table_lists_the_six_models() -> None:
    """Haiku 5.5, Sonnet 5.5, Opus 5.5, Opus 5, Fable 5.2 and 5.1; settings refuse any other
    (tests/unit/test_config_claude.py)."""
    assert ({"claude-haiku-5-5", "claude-opus-5-5"} <= MODEL_PRICES.keys(), len(MODEL_PRICES)) == (
        True,
        6,
    )


def test_c5_a_reservation_counts_4800_image_tokens() -> None:
    """The ceiling of a call with no prompt and no output: the photo's 4,800 input tokens."""
    assert call_ceiling_usd("claude-haiku-5-5", 0, 0) == Decimal("0.00048")


def test_c6_an_unpriced_model_has_no_cost() -> None:
    """No fallback rate: settings refuse such a model, so pricing one is a programming error."""
    with pytest.raises(ValueError, match="claude-unknown-9"):
        call_cost_usd("claude-unknown-9", 1500, 700)
