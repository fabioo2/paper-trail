"""``paper-trail items correct`` and the memory path of experiment E2, end to end (S4-AC-06,
AC-C-03, CLI form).

Pinned (docs/tests/agent-core-C.md):
- ``paper-trail items correct RECEIPT_ID POSITION (--product-id N | --new-product NAME)
  [--category C] [--run NAME]``; POSITION is the line's stored 0-based ``position`` (as
  ``receipts show`` prints it). Prints ``receipt <id> line <position>: product <product id>
  <name>``. Needs no LLM host.
- Exit codes: 0 corrected; 2 neither or both of ``--product-id``/``--new-product``, or an unknown
  category (Typer); 1 an unknown receipt, line or product, or the main database not migrated
  (the ``db upgrade`` hint).
- The terminal row of the ``save_matches`` step counts the sources (``alias 3``).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.agent_cli import (
    StartOllama,
    configure_cli,
    fake_ollama_env,
    invoke,
    rows,
    run_database,
)
from tests.extractions import COSTCO_OCR, EGGS_ALIAS, extraction

RUN = "e2"


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    with fake_ollama_env(monkeypatch) as start:
        yield start


@pytest.fixture
def two_receipts(data_dir: Path, ollama: StartOllama, tmp_path: Path) -> Path:
    """Receipts 1 and 2: the same Costco text, so 2 hits the aliases 1 created; the database."""
    ollama(extraction(), extraction())
    for name in ("a.txt", "b.txt"):
        text = tmp_path / name
        text.write_text(COSTCO_OCR, encoding="utf-8")
        result = invoke("process", "--no-color", "--run", RUN, "--text-file", str(text))
        assert result.exit_code == 0, result.output
    return run_database(data_dir, RUN)


def _sources(database: Path, receipt_id: int) -> list[str | None]:
    found = rows(
        database,
        "SELECT match_source FROM line_items WHERE receipt_id = ? ORDER BY position",
        receipt_id,
    )
    return [source for (source,) in found]


def _correct(*args: str) -> tuple[int, str]:
    result = invoke("items", "correct", *args, "--run", RUN)
    return result.exit_code, result.output


def test_ac_c_03_the_second_receipt_matched_by_alias(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    fake = ollama(extraction(), extraction())
    outputs = []
    for name in ("a.txt", "b.txt"):
        text = tmp_path / name
        text.write_text(COSTCO_OCR, encoding="utf-8")
        result = invoke("process", "--no-color", "--run", RUN, "--text-file", str(text))
        assert result.exit_code == 0, result.output
        outputs.append(result.output)

    database = run_database(data_dir, RUN)
    assert _sources(database, 1) == ["new", "new", "new"]
    assert _sources(database, 2) == ["alias", "alias", "alias"]
    assert len(fake.embed_requests) == 4  # receipt 1 only: 3 searches, 1 batch of vectors
    assert fake.calls == 2
    assert "alias 3" in outputs[1]
    assert "save_matches" in outputs[1]


def test_s4_ac_06_correct_with_a_new_product_then_memory(
    two_receipts: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    code, output = _correct("2", "0", "--new-product", "Kirkland Organic Eggs 24")

    assert code == 0, output
    ((product_id, name),) = rows(
        two_receipts,
        "SELECT p.id, p.canonical_name FROM products p JOIN line_items l ON l.product_id = p.id"
        " WHERE l.receipt_id = 2 AND l.position = 0",
    )
    assert name == "Kirkland Organic Eggs 24"
    assert f"product {product_id}" in output
    assert name in output
    assert _sources(two_receipts, 2)[0] == "memory"
    assert rows(
        two_receipts,
        "SELECT subject, store_key, new_value FROM corrections WHERE kind = 'product_link'",
    ) == [(EGGS_ALIAS, "costco", str(product_id))]
    assert rows(
        two_receipts,
        "SELECT source FROM product_aliases WHERE alias = ? AND store_key = 'costco'",
        EGGS_ALIAS,
    ) == [("user_correction",)]

    fake = ollama(extraction())
    text = tmp_path / "c.txt"
    text.write_text(COSTCO_OCR, encoding="utf-8")
    result = invoke("process", "--no-color", "--run", RUN, "--text-file", str(text))

    assert result.exit_code == 0, result.output
    assert _sources(two_receipts, 3) == ["memory", "alias", "alias"]
    assert fake.embed_requests == []
    ((linked,),) = rows(
        two_receipts, "SELECT product_id FROM line_items WHERE receipt_id = 3 AND position = 0"
    )
    assert linked == product_id


def test_correct_with_an_existing_product_and_a_category(two_receipts: Path) -> None:
    ((milk,),) = rows(
        two_receipts, "SELECT product_id FROM line_items WHERE receipt_id = 1 AND position = 1"
    )

    code, output = _correct("2", "0", "--product-id", str(milk), "--category", "dairy_eggs")

    assert code == 0, output
    assert rows(
        two_receipts, "SELECT product_id FROM line_items WHERE receipt_id = 2 AND position = 0"
    ) == [(milk,)]
    assert rows(two_receipts, "SELECT new_value FROM corrections WHERE kind = 'category'") == [
        ("dairy_eggs",)
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("2", "0"),
        ("2", "0", "--product-id", "1", "--new-product", "Eggs"),
        ("2", "0", "--product-id", "1", "--category", "groceries"),
    ],
    ids=["neither", "both", "unknown-category"],
)
def test_usage_errors_are_exit_2(two_receipts: Path, args: tuple[str, ...]) -> None:
    code, output = _correct(*args)

    assert code == 2, output
    assert "No such" not in output  # refused by the command, not unknown to the CLI
    assert rows(two_receipts, "SELECT count(*) FROM corrections") == [(0,)]


@pytest.mark.parametrize(
    "args",
    [
        ("9", "0", "--product-id", "1"),
        ("2", "9", "--product-id", "1"),
        ("2", "0", "--product-id", "999"),
    ],
    ids=["receipt", "line", "product"],
)
def test_unknown_things_are_exit_1(two_receipts: Path, args: tuple[str, ...]) -> None:
    code, output = _correct(*args)

    assert code == 1, output
    assert rows(two_receipts, "SELECT count(*) FROM corrections") == [(0,)]


def test_the_main_database_must_be_migrated(data_dir: Path) -> None:
    result = invoke("items", "correct", "1", "0", "--product-id", "1")

    assert result.exit_code == 1, result.output
    assert "db upgrade" in result.output
