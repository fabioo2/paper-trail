"""``items correct`` relinks, remembers and learns the code in one unit of work on a real
database file (barcode-lookup plan R10, PC-TC-76): when learning the code fails, nothing of the
correction is kept.
"""

from typing import Self

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.domain.enums import CorrectionKind, MatchSource
from paper_trail.domain.models import ProductCode
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.agent_harness import build_harness
from tests.extractions import C1, NOW, STORE_KEY, STORE_NAME, coded_answer
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.storage import sqlite_uow_factory
from tests.support import require_id


class CodeWriteError(RuntimeError):
    """The product-code repository failed mid-correction."""


class _FailingUpsert:
    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def upsert(self, code: ProductCode, *, replace: bool) -> ProductCode:
        msg = f"cannot write the code {code.code}"
        raise CodeWriteError(msg)


class _CodesFailUnit(SqliteUnitOfWork):
    def __enter__(self) -> Self:
        unit = super().__enter__()
        unit.product_codes = _FailingUpsert(unit.product_codes)  # type: ignore[assignment]
        return unit


def _failing_codes(engine: Engine, clock: FakeClock) -> UnitOfWorkFactory:
    def factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        return _CodesFailUnit(engine, clock, read_only=read_only)

    return factory


def test_pc_tc_76_a_failed_code_write_rolls_the_whole_correction_back(
    migrated_engine: Engine,
) -> None:
    clock = FakeClock(NOW)
    working = build_harness(
        ScriptedReader(coded_answer([C1], store_name=STORE_NAME)),
        clock=clock,
        uow_factory=sqlite_uow_factory(migrated_engine, clock),
    )
    receipt_id = working.process_image().receipt_id
    printed = require_id(working.line(receipt_id, 0).product_id)
    with working.uow_factory(read_only=True) as uow:
        codes_before = uow.product_codes.list()
    aliases_before = working.aliases(printed)
    prices_before = working.price_history(printed)
    failing = build_harness(
        ScriptedReader(), clock=clock, uow_factory=_failing_codes(migrated_engine, clock)
    )

    with pytest.raises(CodeWriteError):
        failing.correct(receipt_id, 0, new_product_name="Choc Pistachio Bars")

    line = working.line(receipt_id, 0)
    assert (line.product_id, line.match_source) == (printed, MatchSource.NEW)
    alias = normalize_alias(C1)
    assert working.latest_correction(alias, CorrectionKind.PRODUCT_LINK, STORE_KEY) is None
    assert working.aliases(printed) == aliases_before
    assert working.price_history(printed) == prices_before
    assert working.product_ids() == [printed]
    with working.uow_factory(read_only=True) as uow:
        assert uow.product_codes.list() == codes_before
