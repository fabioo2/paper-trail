"""What a Claude call costs and whether the month's budget allows another (claude-extraction
plan R2, E7).

Prices are USD per million tokens, per model id (standard rates, prompts of at most 100K
tokens); a model missing from the table is refused when the settings load, so every call has a
price. The month is the UTC calendar month. The budget is checked before each call: a call that
starts under the cap may overshoot it, the next one is stopped; a cap of 0 stops every call.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


MODEL_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": ModelPrice(Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5-5": ModelPrice(Decimal("4.00"), Decimal("20.00")),
    "claude-opus-5": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-fable-5-2": ModelPrice(Decimal("8.00"), Decimal("40.00")),
    "claude-fable-5-1": ModelPrice(Decimal("10.00"), Decimal("50.00")),
}


def call_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """The exact cost of one call's tokens on ``model``; ValueError for a model without a
    price (the settings refuse one, so a call never has one)."""
    price = MODEL_PRICES.get(model)
    if price is None:
        msg = f"no price for the model {model!r}; known: {', '.join(MODEL_PRICES)}"
        raise ValueError(msg)
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / _MILLION


# Most input tokens one receipt photo can cost, as the reader sends it (long edge at most
# 2576 px): about width x height / 750, with room to spare.
IMAGE_INPUT_TOKENS = 4800


def call_ceiling_usd(model: str, prompt_chars: int, max_tokens: int) -> Decimal:
    """The most one read can cost: the photo, every prompt character as a token (a token is
    never shorter than a character) and ``max_tokens`` of output."""
    return call_cost_usd(model, IMAGE_INPUT_TOKENS + prompt_chars, max_tokens)


def month_start(now: datetime) -> datetime:
    """Midnight UTC on the first day of ``now``'s UTC month (``now`` is timezone-aware)."""
    utc = now.astimezone(UTC)
    return datetime(utc.year, utc.month, 1, tzinfo=UTC)


def budget_reached(spent_usd: Decimal, cap_usd: Decimal) -> bool:
    """True when no further call may start this month: ``spent_usd >= cap_usd``."""
    return spent_usd >= cap_usd
