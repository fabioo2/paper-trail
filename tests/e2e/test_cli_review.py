"""``paper-trail review list | show | resolve`` end to end (AC-A-08, S3-AC-07 CLI form).

A receipt is first parked for review by ``process`` (scripted fake Ollama), then resolved by a
separate CLI invocation, which resumes the checkpointed LangGraph thread.

Pinned (docs/tests/agent-core-A.md):
- every ``review`` command takes ``--run NAME`` like ``process`` (default: the main database);
- ``review list`` prints one line per OPEN item with ``#<review id>``, ``receipt <id>`` and the
  reason; ``review show ID`` prints the reason, every issue message and the pending extraction's
  total; ``review resolve ID (--file F | --accept) [--force --reason TEXT]`` prints
  ``receipt <id>: completed``;
- F uses the ``receipts import`` format (``ReceiptImport``, PLAN 3.8), reused, not copied;
- exit codes: 0 resolved; 2 invalid input (an inconsistent draft without ``--force``, listing
  its issues; ``--force`` without ``--reason``; both or neither of ``--file``/``--accept``; an
  invalid file, naming the field); 1 an unknown review or one that is no longer open.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.agent_cli import (
    StartOllama,
    configure_cli,
    fake_ollama_env,
    invoke,
    receipt_status,
    rows,
    run_database,
    write_json,
)
from tests.extractions import (
    COSTCO_OCR,
    LINE1_ISSUE,
    SUBTOTAL_ISSUE,
    extraction,
    inconsistent_extraction,
)
from tests.fakes.ollama_server import FakeOllama

RUN = "rv"

CORRECTED: dict[str, Any] = {
    "store_name": "COSTCO WHOLESALE #1234",
    "purchased_on": "2026-11-01",
    "subtotal_cents": 2555,
    "tax_cents": 128,
    "total_cents": 2683,
    "items": [
        {"raw_text": "KS ORG EGGS 24CT", "unit_price_cents": 899, "line_total_cents": 899},
        {
            "raw_text": "KS 2% MILK 4L",
            "quantity": "2",
            "unit_price_cents": 329,
            "line_total_cents": 658,
        },
        {
            "raw_text": "KS SOURDOUGH",
            "quantity": "2",
            "unit_price_cents": 499,
            "line_total_cents": 998,
        },
    ],
}


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    with fake_ollama_env(monkeypatch) as start:
        yield start


def _park(tmp_path: Path, ollama: StartOllama, *replies: Any, then: Any = None) -> FakeOllama:
    """Run `process` on the Costco text so that it ends in review #1 (receipt 1)."""
    text = tmp_path / "costco.txt"
    text.write_text(COSTCO_OCR, encoding="utf-8")
    fake = ollama(*replies, then=then)
    result = invoke("process", "--no-color", "--run", RUN, "--text-file", str(text))
    assert result.exit_code == 0, result.output
    assert "NEEDS_REVIEW #1" in result.output
    return fake


@pytest.fixture
def parked(data_dir: Path, tmp_path: Path, ollama: StartOllama) -> FakeOllama:
    """Review #1: VALIDATION_FAILED, last issues LINE1_ISSUE and SUBTOTAL_ISSUE."""
    return _park(tmp_path, ollama, inconsistent_extraction(), then=inconsistent_extraction(line=1))


def _review(*args: str) -> tuple[int, str]:
    result = invoke("review", *args, "--run", RUN)
    return result.exit_code, result.output


def _review_status(data_dir: Path, review_id: int = 1) -> str:
    database = run_database(data_dir, RUN)
    ((status,),) = rows(database, "SELECT status FROM review_items WHERE id = ?", review_id)
    return str(status)


def test_review_list_shows_the_open_item(parked: FakeOllama) -> None:
    code, output = _review("list")

    assert code == 0, output
    assert "#1" in output
    assert "receipt 1" in output
    assert "validation_failed" in output


def test_review_show_prints_the_issues_and_the_pending_extraction(parked: FakeOllama) -> None:
    code, output = _review("show", "1")

    assert code == 0, output
    assert "validation_failed" in output
    assert LINE1_ISSUE in output
    assert SUBTOTAL_ISSUE in output
    assert "26.83" in output


def test_ac_a_08_resolve_with_a_corrected_file_resumes_and_completes(
    parked: FakeOllama, data_dir: Path, tmp_path: Path
) -> None:
    """AC-A-08: the same thread resumes (no new model call) and the receipt completes."""
    calls = parked.calls
    corrected = write_json(tmp_path / "corrected.json", CORRECTED)

    code, output = _review("resolve", "1", "--file", str(corrected))

    assert code == 0, output
    assert "receipt 1: completed" in output
    assert parked.calls == calls
    database = run_database(data_dir, RUN)
    assert receipt_status(database, 1) == "completed"
    assert _review_status(data_dir) == "resolved"
    assert rows(database, "SELECT line_total_cents FROM line_items ORDER BY position") == [
        (899,),
        (658,),
        (998,),
    ]


def test_ac_a_08_an_inconsistent_file_is_refused_with_its_issues(
    parked: FakeOllama, data_dir: Path, tmp_path: Path
) -> None:
    wrong = dict(CORRECTED, total_cents=2600)
    path = write_json(tmp_path / "wrong.json", wrong)

    code, output = _review("resolve", "1", "--file", str(path))

    assert code == 2, output
    assert "but the total is 26.00" in output
    assert _review_status(data_dir) == "open"
    assert receipt_status(run_database(data_dir, RUN), 1) == "needs_review"


def test_ac_a_08_force_with_a_reason_accepts_an_inconsistent_file(
    parked: FakeOllama, data_dir: Path, tmp_path: Path
) -> None:
    path = write_json(tmp_path / "wrong.json", dict(CORRECTED, total_cents=2600))

    code, output = _review("resolve", "1", "--file", str(path), "--force", "--reason", "torn")

    assert code == 0, output
    assert receipt_status(run_database(data_dir, RUN), 1) == "completed"
    assert _review_status(data_dir) == "resolved"


def test_ac_a_08_force_without_a_reason_is_a_usage_error(
    parked: FakeOllama, data_dir: Path, tmp_path: Path
) -> None:
    path = write_json(tmp_path / "wrong.json", dict(CORRECTED, total_cents=2600))

    code, output = _review("resolve", "1", "--file", str(path), "--force")

    assert code == 2, output
    assert _review_status(data_dir) == "open"


def test_ac_a_08_accept_needs_force_for_an_inconsistent_extraction(
    parked: FakeOllama, data_dir: Path
) -> None:
    code, output = _review("resolve", "1", "--accept")

    assert code == 2, output
    assert LINE1_ISSUE in output
    assert _review_status(data_dir) == "open"


def test_ac_a_08_accept_resolves_a_low_confidence_review(
    data_dir: Path, tmp_path: Path, ollama: StartOllama
) -> None:
    """AC-A-08: --accept resumes with the stored extraction."""
    fake = _park(tmp_path, ollama, extraction(confidence=0.2))

    code, output = _review("resolve", "1", "--accept")

    assert code == 0, output
    assert fake.calls == 1
    database = run_database(data_dir, RUN)
    assert receipt_status(database, 1) == "completed"
    assert rows(database, "SELECT total_cents, purchased_on FROM receipts") == [
        (2683, "2026-11-01")
    ]


@pytest.mark.parametrize(
    "args",
    [("resolve", "1"), ("resolve", "1", "--accept", "--file", "corrected.json")],
    ids=["neither", "both"],
)
def test_ac_a_08_exactly_one_of_file_or_accept(
    parked: FakeOllama,
    data_dir: Path,
    tmp_path: Path,
    args: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(tmp_path / "corrected.json", CORRECTED)
    monkeypatch.chdir(tmp_path)

    code, output = _review(*args)

    assert code == 2, output
    assert _review_status(data_dir) == "open"


def test_ac_a_08_an_invalid_file_names_the_field(
    parked: FakeOllama, data_dir: Path, tmp_path: Path
) -> None:
    path = write_json(tmp_path / "bad.json", dict(CORRECTED, total_cents=26.83))

    code, output = _review("resolve", "1", "--file", str(path))

    assert code == 2, output
    assert "total_cents" in output
    assert _review_status(data_dir) == "open"


def test_unknown_and_closed_reviews_exit_1(parked: FakeOllama, tmp_path: Path) -> None:
    corrected = write_json(tmp_path / "corrected.json", CORRECTED)

    assert _review("resolve", "9", "--file", str(corrected))[0] == 1
    assert _review("resolve", "1", "--file", str(corrected))[0] == 0
    code, output = _review("resolve", "1", "--file", str(corrected))
    assert code == 1, output
    assert _review("show", "9")[0] == 1
