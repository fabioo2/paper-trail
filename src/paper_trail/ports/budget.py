"""What the Claude calls cost, recorded per call (claude-extraction plan R2, E7, review R4).

A call is admitted by a reservation at its largest possible cost, taken in one write
transaction that also checks the month's spend, so concurrent processes cannot all pass the
check at once: at most one call can overshoot the cap. The reservation then becomes the call's
real cost, or is released when the call failed unbilled.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ClaudeCall:
    at: datetime  # UTC
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal  # ``call_cost_usd`` of the tokens


class SpendLedger(Protocol):
    def reserve(
        self, at: datetime, model: str, cost_usd: Decimal, *, since: datetime, cap_usd: Decimal
    ) -> int | None:
        """Atomically: when every call and reservation at or after ``since`` costs less than
        ``cap_usd``, reserve ``cost_usd`` for a call and return the reservation's id; else
        None (nothing written)."""
        ...

    def settle(self, reservation_id: int, call: ClaudeCall) -> None:
        """Replace the reservation with the call's real tokens and cost; it keeps the time it
        was reserved at (a call counts in the month it started)."""
        ...

    def release(self, reservation_id: int) -> None:
        """Drop the reservation of a call that was not billed."""
        ...

    def spent_since(self, start: datetime) -> Decimal:
        """The cost of every call and open reservation at or after ``start``; 0 when none."""
        ...
