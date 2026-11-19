"""``paper-trail review list | show | resolve`` end to end (AC-A-08, S3-AC-07 CLI form).

A receipt is first parked for review by ``process`` (a scripted fake Claude whose two reads
both fail validation), then resolved by a
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
    StartClaude,
    configure_cli,
    fake_claude_env,
    invoke,
    photo,
    receipt_status,
    rows,
    run_database,
    write_json,
)
from tests.extractions import codex_answer, inconsistent_answer
from tests.fakes.anthropic_server import FakeClaude

RUN = "rv"
FORCED = ("--force", "--reason", "checked the paper")
# What the validator says about inconsistent_answer(): the items add up to 25.46.
SHORT_SUM = "25.46"

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
    return configure_cli(monkeypatch, tmp_path, main_migrated=True)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartClaude]:
    with fake_claude_env(monkeypatch) as start:
        yield start


@pytest.fixture
def parked(data_dir: Path, tmp_path: Path, claude: StartClaude) -> FakeClaude:
    """Review #1 of receipt 1: VALIDATION_FAILED after both reads came back 9 cents short."""
    fake = claude(inconsistent_answer(), inconsistent_answer())
    result = invoke("process", "--no-color", "--run", RUN, str(photo(tmp_path)))
    assert result.exit_code == 0, result.output
    assert "NEEDS_REVIEW #1" in result.output
    return fake


def _review(*args: str) -> tuple[int, str]:
    result = invoke("review", *args, "--run", RUN)
    return result.exit_code, result.output


def _review_status(data_dir: Path, review_id: int = 1) -> str:
    database = run_database(data_dir, RUN)
    ((status,),) = rows(database, "SELECT status FROM review_items WHERE id = ?", review_id)
    return str(status)


def test_review_list_shows_the_open_item(parked: FakeClaude) -> None:
    code, output = _review("list")

    assert code == 0, output
    assert "#1" in output
    assert "receipt 1" in output
    assert "validation_failed" in output


def test_review_show_prints_the_issues_and_the_pending_extraction(parked: FakeClaude) -> None:
    code, output = _review("show", "1")

    assert code == 0, output
    assert "validation_failed" in output
    assert SHORT_SUM in output
    assert "26.83" in output


def test_ac_a_08_resolve_with_a_corrected_file_resumes_and_completes(
    parked: FakeClaude, data_dir: Path, tmp_path: Path
) -> None:
    """AC-A-08 / CE-TC-030: the same thread resumes (no new read) and the receipt completes."""
    calls = parked.calls
    corrected = write_json(tmp_path / "corrected.json", CORRECTED)

    code, output = _review("resolve", "1", "--file", str(corrected), *FORCED)

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
    parked: FakeClaude, data_dir: Path, tmp_path: Path
) -> None:
    wrong = dict(CORRECTED, total_cents=2600)
    path = write_json(tmp_path / "wrong.json", wrong)

    code, output = _review("resolve", "1", "--file", str(path))

    assert code == 2, output
    assert "but the total is 26.00" in output
    assert _review_status(data_dir) == "open"
    assert receipt_status(run_database(data_dir, RUN), 1) == "needs_review"


def test_ac_a_08_force_with_a_reason_accepts_an_inconsistent_file(
    parked: FakeClaude, data_dir: Path, tmp_path: Path
) -> None:
    path = write_json(tmp_path / "wrong.json", dict(CORRECTED, total_cents=2600))

    code, output = _review("resolve", "1", "--file", str(path), "--force", "--reason", "torn")

    assert code == 0, output
    assert receipt_status(run_database(data_dir, RUN), 1) == "completed"
    assert _review_status(data_dir) == "resolved"


def test_ac_a_08_force_without_a_reason_is_a_usage_error(
    parked: FakeClaude, data_dir: Path, tmp_path: Path
) -> None:
    path = write_json(tmp_path / "wrong.json", dict(CORRECTED, total_cents=2600))

    code, output = _review("resolve", "1", "--file", str(path), "--force")

    assert code == 2, output
    assert _review_status(data_dir) == "open"


def test_ac_a_08_accept_needs_force_for_an_inconsistent_extraction(
    parked: FakeClaude, data_dir: Path
) -> None:
    code, output = _review("resolve", "1", "--accept")

    assert code == 2, output
    assert SHORT_SUM in output
    assert _review_status(data_dir) == "open"


def test_ac_a_08_accept_with_force_keeps_the_second_reading(
    parked: FakeClaude, data_dir: Path
) -> None:
    """AC-A-08 / CE-TC-023: --accept resumes with the reading the review holds (the second
    attempt's); no new read."""
    code, output = _review("resolve", "1", "--accept", "--force", "--reason", "short receipt")

    assert code == 0, output
    assert parked.calls == 2
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
    parked: FakeClaude,
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
    parked: FakeClaude, data_dir: Path, tmp_path: Path
) -> None:
    path = write_json(tmp_path / "bad.json", dict(CORRECTED, total_cents=26.83))

    code, output = _review("resolve", "1", "--file", str(path))

    assert code == 2, output
    assert "total_cents" in output
    assert _review_status(data_dir) == "open"


def test_unknown_and_closed_reviews_exit_1(parked: FakeClaude, tmp_path: Path) -> None:
    corrected = write_json(tmp_path / "corrected.json", CORRECTED)

    assert _review("resolve", "9", "--file", str(corrected), *FORCED)[0] == 1
    assert _review("resolve", "1", "--file", str(corrected), *FORCED)[0] == 0
    code, output = _review("resolve", "1", "--file", str(corrected), *FORCED)
    assert code == 1, output
    assert _review("show", "9")[0] == 1


# --- review gaps: accepting as read, and reviews ``receipts import`` opened ------------------


@pytest.mark.parametrize("forced", [False, True], ids=["refused", "forced"])
def test_a1_accept_of_a_reading_whose_unit_price_does_not_add_up(
    data_dir: Path, tmp_path: Path, claude: StartClaude, forced: bool
) -> None:
    """F-CODEX: 2 x 5.00 printed for 8.00. Without --force the original issue is printed
    (exit 2, the review stays open); with --force --reason the reading is saved as read."""
    fake = claude(codex_answer(), codex_answer())
    assert invoke("process", "--no-color", "--run", RUN, str(photo(tmp_path))).exit_code == 0
    calls = fake.calls
    force = ("--force", "--reason", "per pack") if forced else ()

    code, output = _review("resolve", "1", "--accept", *force)

    database = run_database(data_dir, RUN)
    assert fake.calls == calls
    if forced:
        assert code == 0, output
        assert receipt_status(database, 1) == "completed"
    else:
        assert code == 2, output
        assert "2 each x 5.00 = 10.00" in output
        assert "--force --reason" in output
        assert _review_status(data_dir) == "open"


def test_i09_resolve_answers_a_review_receipts_import_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = configure_cli(monkeypatch, tmp_path, main_migrated=True)
    wrong = write_json(tmp_path / "wrong.json", dict(CORRECTED, total_cents=2600))
    corrected = write_json(tmp_path / "corrected.json", CORRECTED)
    assert invoke("receipts", "import", str(wrong)).exit_code == 0

    result = invoke("review", "resolve", "1", "--file", str(corrected), *FORCED)

    assert result.exit_code == 0, result.output
    assert "receipt 1: completed" in result.output
    database = run_database(data_dir, None)
    assert receipt_status(database, 1) == "completed"
    assert rows(database, "SELECT status FROM review_items") == [("resolved",)]
    assert rows(database, "SELECT total_cents FROM receipts") == [(2683,)]


def test_review_show_prints_an_imported_review_s_receipt_as_its_reading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_cli(monkeypatch, tmp_path, main_migrated=True)
    wrong = write_json(tmp_path / "wrong.json", dict(CORRECTED, total_cents=2600))
    assert invoke("receipts", "import", str(wrong)).exit_code == 0

    result = invoke("review", "show", "1")

    assert result.exit_code == 0, result.output
    assert "pending extraction: COSTCO WHOLESALE #1234 · 3 lines · total 26.00 CAD" in result.output
