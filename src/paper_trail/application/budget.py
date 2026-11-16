"""The monthly Claude budget around a ``ReceiptReader`` (claude-extraction plan R2, E7, review
R4).

A read is up to ``ATTEMPTS`` attempts: the inner reader makes one HTTP request each, and an
attempt that failed retryably (a rate limit, an overloaded or failing API, a lost connection,
a timeout) is made again after a short backoff (review C11).

Before each attempt a reservation at its largest possible cost (``call_ceiling_usd``) is taken
in the ledger, in one transaction with the check of the month's spend (UTC calendar month):
when the spend and the open reservations have reached the cap, ``BudgetReachedError`` is raised
and no request is made. Concurrent processes are admitted one at a time. After the attempt its
reservation becomes its real cost (a failed but billed attempt included); it is released only
when the attempt was certainly not billed (an error status, a refused connection, nothing
sent). An attempt that may have been billed without saying what it cost (a timeout, a broken
connection, a crash) is charged at its ceiling, so the month can overshoot the cap by one
attempt's ceiling at most.
"""

import time
from collections.abc import Callable
from decimal import Decimal

from paper_trail.domain.rules.budget import (
    budget_reached,
    call_ceiling_usd,
    call_cost_usd,
    month_start,
)
from paper_trail.ports.budget import ClaudeCall, SpendLedger
from paper_trail.ports.clock import Clock
from paper_trail.ports.reader import (
    BudgetReachedError,
    ReaderError,
    ReadRequest,
    ReadResult,
    ReadUsage,
    ReceiptReader,
)

BUDGET_VARIABLE = "PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD"
ATTEMPTS = 3  # a first attempt and two retries
BACKOFF_S = (1.0, 2.0)  # the wait before the second and the third attempt


class BudgetedReader:
    """Implements ``paper_trail.ports.reader.ReceiptReader`` over ``inner``."""

    def __init__(
        self,
        inner: ReceiptReader,
        ledger: SpendLedger,
        clock: Clock,
        cap_usd: Decimal,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._clock = clock
        self._cap_usd = cap_usd
        self._sleep = sleep

    def check(self) -> None:
        """BudgetReachedError when this month's spend has reached the cap (no call, nothing
        reserved): what a caller checks before it changes anything for a read."""
        spent = self._ledger.spent_since(month_start(self._clock.now()))
        if budget_reached(spent, self._cap_usd):
            raise BudgetReachedError(self._reached(spent))

    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        for backoff in BACKOFF_S[: ATTEMPTS - 1]:
            try:
                return self._attempt(request, schema)
            except ReaderError as error:
                if not error.retryable:
                    raise
            self._sleep(backoff)
        return self._attempt(request, schema)

    def _attempt[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        """One request, reserved before and settled or released after."""
        now = self._clock.now()
        ceiling = call_ceiling_usd(
            request.model, len(request.system) + len(request.text), request.max_tokens
        )
        since = month_start(now)
        reservation = self._ledger.reserve(
            now, request.model, ceiling, since=since, cap_usd=self._cap_usd
        )
        if reservation is None:
            raise BudgetReachedError(self._reached(self._ledger.spent_since(since)))
        try:
            result = self._inner.read(request, schema)
        except ReaderError as error:
            if error.usage is not None:
                self._settle(reservation, request.model, error.usage)
            elif error.maybe_billed:
                self._charge_ceiling(reservation, request.model, ceiling)
            else:
                self._ledger.release(reservation)
            raise
        except BaseException:
            self._charge_ceiling(reservation, request.model, ceiling)
            raise
        self._settle(reservation, request.model, result.usage)
        return result

    def _settle(self, reservation: int, model: str, usage: ReadUsage) -> None:
        self._ledger.settle(
            reservation,
            ClaudeCall(
                at=self._clock.now(),
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=call_cost_usd(model, usage.input_tokens, usage.output_tokens),
            ),
        )

    def _charge_ceiling(self, reservation: int, model: str, ceiling: Decimal) -> None:
        """An attempt that may have been billed, at the most it can cost."""
        self._ledger.settle(
            reservation,
            ClaudeCall(
                at=self._clock.now(),
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=ceiling,
            ),
        )

    def _reached(self, spent: Decimal) -> str:
        return (
            f"monthly Claude budget reached (spent {spent:.2f} of {self._cap_usd:.2f} USD);"
            f" raise {BUDGET_VARIABLE} or wait for next month"
        )
