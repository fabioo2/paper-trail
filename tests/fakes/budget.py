"""In-memory spend ledger (implements ``paper_trail.ports.budget.SpendLedger``): billed calls
in ``calls``, open reservations in ``reservations`` (review R4)."""

from datetime import datetime
from decimal import Decimal

from paper_trail.ports.budget import ClaudeCall


class FakeSpendLedger:
    def __init__(self, *calls: ClaudeCall) -> None:
        self.calls: list[ClaudeCall] = list(calls)
        self.reservations: dict[int, tuple[datetime, Decimal]] = {}
        self._next_id = 1

    def reserve(
        self, at: datetime, model: str, cost_usd: Decimal, *, since: datetime, cap_usd: Decimal
    ) -> int | None:
        if self.spent_since(since) >= cap_usd:
            return None
        reservation_id = self._next_id
        self._next_id += 1
        self.reservations[reservation_id] = (at, cost_usd)
        return reservation_id

    def settle(self, reservation_id: int, call: ClaudeCall) -> None:
        del self.reservations[reservation_id]
        self.calls.append(call)

    def release(self, reservation_id: int) -> None:
        del self.reservations[reservation_id]

    def spent_since(self, start: datetime) -> Decimal:
        booked = [call.cost_usd for call in self.calls if call.at >= start]
        booked += [cost for at, cost in self.reservations.values() if at >= start]
        return sum(booked, Decimal(0))
