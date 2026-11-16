"""No socket is opened by the catalog (barcode-lookup PR 1a, R12; PC-TC-81).

Under a guard that makes ``socket.socket.connect`` and ``socket.getaddrinfo`` raise, the search
link, both catalogs, the ``catalog_lookup`` tool and a whole in-process run of a coded receipt
succeed. PR 1b adds the ``catalog`` use cases: ``todo`` (with its Costco links), ``add``,
``lookup``, ``plu-import`` and ``backfill --apply`` (PC-TC-108, 160).
The static half is ``tests/repo/test_catalog_no_network.py``.
"""

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from paper_trail.adapters.catalog.composite import CompositeCatalog
from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.application.catalog import (
    AddCatalogCode,
    BackfillCatalog,
    CatalogTodo,
    CodeNaming,
    LookupCatalogCode,
    import_plu,
)
from paper_trail.domain.enums import MatchSource, ReceiptStatus
from paper_trail.domain.rules.item_codes import ItemCode, lookup_url, parse_item_code
from tests.agent_harness import build_harness
from tests.extractions import C1, L3, LOBLAWS, LOBLAWS_KEY, PLU_CSV, coded_answer
from tests.factories import item_line, legacy_product
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.storage import in_unit, seed_purchase
from tests.support import require_id


class NetworkUsedError(AssertionError):
    """Something tried to reach the network."""


def _refuse(*_: Any, **__: Any) -> Any:
    raise NetworkUsedError("network access attempted")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


@pytest.fixture
def plu_path(tmp_path: Path) -> Path:
    path = tmp_path / "plu.csv"
    path.write_text(PLU_CSV, encoding="utf-8")
    return path


def _l3() -> ItemCode:
    code = parse_item_code(L3, LOBLAWS_KEY)
    assert code is not None
    return code


@pytest.mark.usefixtures("no_network")
def test_the_guard_refuses_a_connection() -> None:
    with pytest.raises(NetworkUsedError):
        socket.create_connection(("www.costco.ca", 443), timeout=1)


@pytest.mark.usefixtures("no_network")
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (
            lambda _: lookup_url("costco", "1362394"),
            "https://www.costco.ca/CatalogSearch?keyword=1362394",
        ),
        (lambda path: PluCatalog(path).lookup(_l3(), LOBLAWS_KEY), TEST_APPLE),
        (
            lambda path: CompositeCatalog(FakeCatalog(), PluCatalog(path)).lookup(
                _l3(), LOBLAWS_KEY
            ),
            TEST_APPLE,
        ),
    ],
    ids=["lookup-url", "plu-catalog", "composite-catalog"],
)
def test_the_catalog_works_offline(
    plu_path: Path, action: Callable[[Path], object], expected: object
) -> None:
    assert action(plu_path) == expected


@pytest.mark.usefixtures("no_network")
def test_a_whole_run_of_a_coded_receipt_works_offline(plu_path: Path) -> None:
    reader = ScriptedReader(coded_answer([C1, L3], store_name=LOBLAWS))
    harness = build_harness(reader, catalog=PluCatalog(plu_path))

    outcome = harness.process_image()

    assert outcome.failure is None
    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    assert reader.calls == 1


def _catalog_actions(tmp_path: Path, plu_path: Path) -> dict[str, Callable[[], object]]:
    """Each ``catalog`` use case over a legacy product bought twice as C1 at Costco."""
    clock = FakeClock()
    factory = fake_uow_factory(FakeDatabase(), clock)
    legacy = require_id(in_unit(factory, lambda uow: uow.products.add(legacy_product())).id)
    seed_purchase(
        factory,
        [item_line(p, C1, 1699, product_id=legacy, match_source=MatchSource.LLM) for p in (0, 1)],
    )
    export = tmp_path / "ifps.csv"
    export.write_text("Plu,Commodity,Variety,Size\n3101,TEST APPLE,Red,Small\n", encoding="utf-8")
    return {
        "backfill": lambda: BackfillCatalog(factory, clock, PluCatalog(plu_path))(apply=True),
        "todo": CatalogTodo(factory),
        "add": lambda: AddCatalogCode(factory, clock)(CodeNaming("costco", "1543210", name="X")),
        "lookup": lambda: LookupCatalogCode(PluCatalog(plu_path))(L3, LOBLAWS),
        "plu-import": lambda: import_plu(export, tmp_path / "catalog" / "plu.csv"),
    }


@pytest.mark.usefixtures("no_network")
def test_pc_tc_108_the_catalog_commands_work_offline(tmp_path: Path, plu_path: Path) -> None:
    """In this order, so ``todo`` prints the Costco link of the code the backfill attached."""
    results = {name: action() for name, action in _catalog_actions(tmp_path, plu_path).items()}

    todo = results["todo"]
    assert isinstance(todo, tuple)
    assert [row.lookup_url for row in todo] == [lookup_url("costco", "1362394")]
