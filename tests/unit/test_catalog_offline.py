"""No socket is opened by the catalog (barcode-lookup PR 1a, R12; PC-TC-81).

Under a guard that makes ``socket.socket.connect`` and ``socket.getaddrinfo`` raise, the search
link, both catalogs, the ``catalog_lookup`` tool and a whole in-process run of a coded receipt
succeed. PR 1b adds ``catalog todo``, ``add --yes`` and ``backfill`` to the table (PC-TC-108).
The static half is ``tests/repo/test_catalog_no_network.py``.
"""

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from paper_trail.adapters.catalog.composite import CompositeCatalog
from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.rules.item_codes import ItemCode, lookup_url, parse_item_code
from tests.agent_harness import build_harness
from tests.extractions import C1, L3, LOBLAWS, LOBLAWS_KEY, PLU_CSV, coded_extraction
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.llm import ScriptedLlm


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
    harness = build_harness(
        ScriptedLlm(coded_extraction([C1, L3], store_name=LOBLAWS)),
        catalog=PluCatalog(plu_path),
    )

    outcome = harness.process_image()

    assert outcome.failure is None
    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    assert harness.llm.calls == 1
