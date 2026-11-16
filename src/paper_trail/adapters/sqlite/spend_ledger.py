"""``SpendLedger`` on SQLite: ``claude_calls``, one row per call (claude-extraction plan R2,
review R3, R4). It always lives in the main database, whatever run the agents work in. Each
method is its own short transaction, so a call made inside a receipt's run is counted even
when the run fails; a reservation takes the write lock (``BEGIN IMMEDIATE``) before it reads
the month's spend, so two processes never both pass the cap check.

A reservation still open after ``abandon_after`` belongs to a process that died between its
reservation and its settlement (no attempt takes that long: one HTTP request gives up after
its timeout; the window keeps the margin of a whole read, three attempts and their backoff):
it no longer counts, neither for admission nor in the spend (review R15)."""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Connection, Engine, and_, delete, insert, not_, select, update

from paper_trail.adapters.clock import SystemClock
from paper_trail.adapters.sqlite.engine import begin_immediate, begin_read_only
from paper_trail.adapters.sqlite.schema import claude_calls
from paper_trail.ports.budget import ClaudeCall
from paper_trail.ports.clock import Clock

# CLAUDE__TIMEOUT_S's default (300 s) x 3 tries + 120 s for the SDK's backoff between them;
# the composition root passes the configured timeout's.
DEFAULT_ABANDON_AFTER = timedelta(seconds=300 * 3 + 120)


class SqliteSpendLedger:
    """Implements ``paper_trail.ports.budget.SpendLedger``."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock | None = None,
        abandon_after: timedelta = DEFAULT_ABANDON_AFTER,
    ) -> None:
        self._engine = engine
        self._clock = clock or SystemClock()
        self._abandon_after = abandon_after

    def record(self, call: ClaudeCall) -> None:
        """Store one billed call outright, without a reservation (seeding, maintenance)."""
        with self._engine.connect() as connection, begin_immediate(connection):
            connection.execute(
                insert(claude_calls).values(
                    at=call.at,
                    model=call.model,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cost_usd=call.cost_usd,
                )
            )

    def reserve(
        self, at: datetime, model: str, cost_usd: Decimal, *, since: datetime, cap_usd: Decimal
    ) -> int | None:
        with self._engine.connect() as connection, begin_immediate(connection):
            if self._spent(connection, since) >= cap_usd:
                return None
            reservation: int = connection.execute(
                insert(claude_calls)
                .values(at=at, model=model, cost_usd=cost_usd)
                .returning(claude_calls.c.id)
            ).scalar_one()
            return reservation

    def settle(self, reservation_id: int, call: ClaudeCall) -> None:
        with self._engine.connect() as connection, begin_immediate(connection):
            # The reservation's ``at`` is kept: a call is counted in the month it started.
            connection.execute(
                update(claude_calls)
                .where(claude_calls.c.id == reservation_id)
                .values(
                    model=call.model,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cost_usd=call.cost_usd,
                )
            )

    def release(self, reservation_id: int) -> None:
        with self._engine.connect() as connection, begin_immediate(connection):
            connection.execute(delete(claude_calls).where(claude_calls.c.id == reservation_id))

    def spent_since(self, start: datetime) -> Decimal:
        with self._engine.connect() as connection, begin_read_only(connection):
            return self._spent(connection, start)

    def _spent(self, connection: Connection, start: datetime) -> Decimal:
        abandoned = and_(
            claude_calls.c.input_tokens.is_(None),
            claude_calls.c.at < self._clock.now() - self._abandon_after,
        )
        # Summed in Python: SQLite would add the exact decimal texts as floats.
        costs = connection.execute(
            select(claude_calls.c.cost_usd).where(claude_calls.c.at >= start, not_(abandoned))
        ).scalars()
        return sum(costs, Decimal(0))
