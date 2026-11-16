"""``paper-trail process`` end to end (AC-A-10, AC-A-09, AC-A-06, AC-A-02; claude-extraction
CE-TC-040, 055, 058).

Real composition root, real SQLite, real Anthropic adapter; the model is a scripted fake
Messages API on 127.0.0.1 (``ANTHROPIC_BASE_URL``), never the real API.

Pinned (docs/tests/agent-core-A.md, claude-extraction test-cases.md):
- ``--run NAME`` uses ``<DATA_DIR>/runs/NAME/paper_trail.db`` (migrated automatically) and
  writes traces to ``<DATA_DIR>/runs/NAME/traces/receipt-<id>-<YYYYMMDDTHHMMSSZ>.json`` and a
  ``summary.json`` beside them; without ``--run`` the main database (which must be migrated,
  else exit 1 with a hint to run ``db upgrade``) and the run name ``main``. NAME matches
  ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``, else exit 2.
- Exit codes: 0 every receipt completed or parked for review; 3 any receipt failed (an
  explicit file that is not an image counts as failed); 4 Claude is unavailable or there is no
  ``ANTHROPIC_API_KEY`` (processing stops, the receipt is FAILED with its checkpoint, the
  hint ``paper-trail process --resume <id>``); 2 usage errors.
- In a directory, files whose extension is not .jpg/.jpeg/.png/.webp/.heic/.heif (any case)
  are listed as skipped; directories are not recursed; files run in name order.
- The input is a photo: ``--text-file`` and the OCR/vision flags are gone (CE-TC-058).
"""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config
from paper_trail.adapters.sqlite.spend_ledger import SqliteSpendLedger
from paper_trail.config import DATABASE_FILENAME
from paper_trail.domain.rules.budget import month_start
from paper_trail.ports.budget import ClaudeCall
from tests.e2e.agent_cli import (
    StartClaude,
    configure_cli,
    fake_claude_env,
    invoke,
    photo,
    receipt_status,
    rows,
    run_database,
    trace_document,
    trace_files,
)
from tests.extractions import (
    GENERIC_RETRY_LINE,
    L3,
    L4,
    LOBLAWS,
    METRO,
    PLU_CSV,
    answer,
    coded_answer,
    inconsistent_answer,
    metro_answer,
)
from tests.fakes.anthropic_server import UNAVAILABLE
from tests.support import CopyMigrated

TRACE_NAME = re.compile(r"receipt-(\d+)-\d{8}T\d{6}Z\.json")
EXTRACT_MODEL = "claude-haiku-5-5"
RETRY_MODEL = "claude-opus-5-5"
# What the validator says about inconsistent_answer(): the items add up to 25.46.
SHORT_SUM = "25.46"


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path, main_migrated=True)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartClaude]:
    with fake_claude_env(monkeypatch) as start:
        yield start


@pytest.fixture
def receipt(tmp_path: Path) -> Path:
    return photo(tmp_path)


def _process(*args: str) -> tuple[int, str]:
    result = invoke("process", "--no-color", *args)
    return result.exit_code, result.output


# --- completed and reviewed receipts (exit 0) ------------------------------------------------


def test_ac_a_10_a_clean_receipt_completes_with_exit_0(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    fake = claude(answer())

    code, output = _process("--run", "e1", str(receipt))

    assert code == 0, output
    assert "receipt 1" in output
    assert "costco.png" in output
    assert "COMPLETED" in output
    assert receipt_status(run_database(data_dir, "e1"), 1) == "completed"
    assert fake.models == [EXTRACT_MODEL]


def test_ce_tc_055_the_metro_photo_end_to_end(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """CE-TC-055: one read; the block shows the store, the date, the total and the five
    savings; the stored lines are 7 items and 5 linked discounts totalling -6.41."""
    fake = claude(metro_answer())

    code, output = _process("--run", "metro", str(receipt))

    assert code == 0, output
    assert fake.calls == 1
    for shown in (METRO, "2026-11-03", "42.04", "6.41"):
        assert shown in output, shown
    database = run_database(data_dir, "metro")
    assert rows(
        database,
        "SELECT kind, count(*), sum(line_total_cents) FROM line_items GROUP BY kind ORDER BY kind",
    ) == [("discount", 5, -641), ("item", 7, 4845)]
    assert rows(
        database,
        "SELECT count(*) FROM line_items WHERE kind = 'discount' AND applies_to_position IS NULL",
    ) == [(0,)]
    assert rows(database, "SELECT store_name, purchased_on, total_cents FROM receipts") == [
        (METRO, "2026-11-03", 4204)
    ]
    (path,) = trace_files(data_dir, "metro")
    assert json.loads(path.read_text("utf-8"))["outcome"]["status"] == "completed"


def test_ac_a_10_a_receipt_parked_for_review_is_exit_0_with_the_resolve_hint(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    fake = claude(then=inconsistent_answer())

    code, output = _process("--run", "rv", str(receipt))

    assert code == 0, output
    assert "NEEDS_REVIEW #1" in output
    assert "paper-trail review resolve 1" in output
    assert receipt_status(run_database(data_dir, "rv"), 1) == "needs_review"
    assert fake.models == [EXTRACT_MODEL, RETRY_MODEL]


def test_r17_the_table_row_of_a_receipt_parked_for_review_shows_its_pending_reading(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """Review R17: nothing is saved yet, so the row is the extraction the review holds:
    Costco's 3 items summing to 25.46 against the printed 25.55 / 26.83, not adding up."""
    claude(then=inconsistent_answer())

    code, output = _process("--run", "tb", "--table", str(receipt))

    assert code == 0, output
    (row,) = [line for line in output.splitlines() if line.startswith(receipt.name[:24])]
    assert row.split()[1:9] == [
        "COSTCO",
        "WHOLESALE",
        "#12",  # the store column is 20 wide
        "3",
        "25.46",
        "25.55",
        "26.83",
        "NO",
    ]


# --- AC-A-09: the JSON trace and the summary ------------------------------------------------


def test_ac_a_09_the_json_trace_validates_and_tells_the_story(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """AC-A-09 / plan 4.3: one trace per receipt, valid paper-trail.trace/v2, totals that
    match the events, one LLM event per read with its model and attempt (CE-TC-031)."""
    claude(inconsistent_answer(), answer())

    code, output = _process("--run", "tr", str(receipt))

    assert code == 0, output
    (path,) = trace_files(data_dir, "tr")
    assert TRACE_NAME.fullmatch(path.name)
    document = trace_document(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == "paper-trail.trace/v2"
    assert (raw["run"], raw["receipt_id"], raw["source_name"]) == ("tr", 1, "costco.png")
    assert raw["image_sha256"] is not None
    assert raw["outcome"]["status"] == "completed"
    assert raw["config"]["feedback"] is True
    assert raw["config"]["transport"] == "inprocess"
    events = raw["events"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    llm_events = [event for event in events if event["kind"] == "llm"]
    assert [(event["data"]["attempt"], event["data"]["model"]) for event in llm_events] == [
        (1, EXTRACT_MODEL),
        (2, RETRY_MODEL),
    ]
    assert all(isinstance(event["duration_ms"], int) for event in llm_events)
    totals = raw["totals"]
    assert totals["llm_calls"] == 2
    assert totals["tool_calls"] == len([event for event in events if event["kind"] == "tool"])
    assert totals["tool_denied"] == 0
    assert document.receipt_id == 1
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # atomic: no temp files left


def test_ac_a_10_the_run_writes_a_summary(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(inconsistent_answer(), answer())

    _process("--run", "sm", str(receipt))

    summary = json.loads((data_dir / "runs" / "sm" / "summary.json").read_text("utf-8"))
    assert summary["run"] == "sm"
    assert (summary["receipts"], summary["completed"], summary["needs_review"]) == (1, 1, 0)
    assert (summary["failed"], summary["skipped"]) == (0, 0)
    assert summary["llm_calls"] == 2


def test_new_products_are_described_from_the_plu_table_and_shown_on_the_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claude: StartClaude, receipt: Path
) -> None:
    """PC-TC-89 (moved from the deleted vision suite): with enrichment on, the two PLU
    products are described from the PLU table by the supervisor; the read is the only call."""
    data_dir = configure_cli(monkeypatch, tmp_path, enrichment=True, main_migrated=True)
    plu = data_dir / "catalog" / "plu.csv"
    plu.parent.mkdir(parents=True, exist_ok=True)
    plu.write_text(PLU_CSV, encoding="utf-8")
    fake = claude(coded_answer([L3, L4], store_name=LOBLAWS))

    code, output = _process("--run", "plu", str(receipt))

    assert code == 0, output
    assert fake.calls == 1
    assert "new 2 (plu 2)" in output
    describe_rows = [line for line in output.splitlines() if "describe_from_catalog" in line]
    assert len(describe_rows) == 2
    assert all("supervisor" in line for line in describe_rows)
    assert rows(
        run_database(data_dir, "plu"),
        "SELECT canonical_name, category, tags FROM products ORDER BY id",
    ) == [("Test Apple", "produce", "[]"), ("Organic Test Apple", "produce", '["organic"]')]
    (path,) = trace_files(data_dir, "plu")
    assert json.loads(path.read_text("utf-8"))["config"]["enrichment"] is True


# --- AC-A-06 flags and settings ----------------------------------------------------------------


def test_ac_a_06_the_retry_carries_the_failures(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    fake = claude(inconsistent_answer(), answer())

    code, output = _process("--run", "fb", str(receipt))

    assert code == 0, output
    assert SHORT_SUM not in "\n".join(fake.texts(0))
    assert SHORT_SUM in "\n".join(fake.texts(1))


def test_ac_a_06_no_feedback_flag(data_dir: Path, claude: StartClaude, receipt: Path) -> None:
    fake = claude(inconsistent_answer(), answer())

    code, output = _process("--run", "gen", "--no-feedback", str(receipt))

    assert code == 0, output
    second = "\n".join(fake.texts(1))
    assert GENERIC_RETRY_LINE in second
    assert SHORT_SUM not in second
    (trace,) = trace_files(data_dir, "gen")
    assert json.loads(trace.read_text("utf-8"))["config"]["feedback"] is False


def test_ce_tc_025_one_attempt_goes_straight_to_review(
    data_dir: Path, claude: StartClaude, receipt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__MAX_ATTEMPTS", "1")
    fake = claude(then=inconsistent_answer())

    code, output = _process("--run", "one", str(receipt))

    assert code == 0, output
    assert fake.models == [EXTRACT_MODEL]
    assert "NEEDS_REVIEW" in output


def test_verbose_prints_the_feedback(data_dir: Path, claude: StartClaude, receipt: Path) -> None:
    claude(inconsistent_answer(), answer())

    code, output = _process("--run", "vb", "--verbose", str(receipt))

    assert code == 0, output
    assert SHORT_SUM in output


@pytest.mark.parametrize(
    "option",
    [
        ("--text-file", "receipt.txt"),
        ("--no-vision",),
        ("--max-text-attempts", "1"),
        ("--prompt-version", "v1"),
        ("--no-examples",),
    ],
    ids=["text-file", "no-vision", "max-text-attempts", "prompt-version", "no-examples"],
)
def test_ce_tc_058_the_ocr_and_vision_options_are_gone(
    data_dir: Path,
    claude: StartClaude,
    receipt: Path,
    option: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(receipt.parent)
    (receipt.parent / "receipt.txt").write_text("TOTAL 1.00", encoding="utf-8")
    fake = claude(answer())

    code, output = _process("--run", "gone", *option, str(receipt))

    assert code == 2, output
    assert "No such option" in output
    assert fake.calls == 0


def test_ce_tc_058_help_lists_only_the_kept_options() -> None:
    result = invoke("process", "--help")

    assert result.exit_code == 0, result.output
    gone_options = (
        "--text-file",
        "--no-vision",
        "--max-text-attempts",
        "--prompt-version",
        "--no-examples",
    )
    for gone in gone_options:
        assert gone not in result.output, gone
    for kept in ("--no-feedback", "--interrupt-after", "--resume", "--reprocess", "--transport"):
        assert kept in result.output, kept


@pytest.mark.parametrize("transport", ["inprocess", "mcp"])
def test_ce_tc_058_the_kept_options_are_accepted(
    data_dir: Path, claude: StartClaude, receipt: Path, transport: str
) -> None:
    claude(answer())

    code, output = _process(
        "--run", "kept", "--no-feedback", "--transport", transport, str(receipt)
    )

    assert code == 0, output
    assert "COMPLETED" in output


# --- images, directories, duplicates --------------------------------------------------------------


def test_ac_a_10_a_directory_skips_non_images(
    data_dir: Path, claude: StartClaude, tmp_path: Path
) -> None:
    """AC-A-10: non-image files in a directory are listed as skipped, not fatal."""
    folder = tmp_path / "receipts"
    folder.mkdir()
    photo(folder, "costco.JPG")
    (folder / "notes.txt").write_text("not a receipt", encoding="utf-8")
    (folder / "README.md").write_text("# receipts", encoding="utf-8")
    (folder / "nested").mkdir()
    fake = claude(answer())

    code, output = _process("--run", "dir", str(folder))

    assert code == 0, output
    assert fake.calls == 1
    assert "costco.JPG" in output
    assert re.search(r"skipped.*notes\.txt", output, re.IGNORECASE | re.DOTALL)
    assert re.search(r"skipped.*README\.md", output, re.IGNORECASE | re.DOTALL)
    database = run_database(data_dir, "dir")
    ((sha,),) = rows(database, "SELECT image_sha256 FROM receipts")
    assert sha is not None
    assert list((data_dir / "images").rglob(f"{sha}.jpg"))


def test_ac_a_02_the_same_photo_twice_is_a_duplicate(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    fake = claude(answer())

    first = _process("--run", "dup", str(receipt))
    second = _process("--run", "dup", str(receipt))

    assert first[0] == second[0] == 0, second[1]
    assert "duplicate" in second[1].lower()
    assert fake.calls == 1
    assert rows(run_database(data_dir, "dup"), "SELECT count(*) FROM receipts") == [(1,)]


def test_ac_a_10_a_file_that_is_not_an_image_fails_with_exit_3(
    data_dir: Path, claude: StartClaude, tmp_path: Path
) -> None:
    good = photo(tmp_path, "a-costco.jpg")
    bad = tmp_path / "b-fake.jpg"
    bad.write_bytes(b"%PDF-1.7 a pdf named like a photo")
    fake = claude(answer())

    code, output = _process("--run", "bad", str(good), str(bad))

    assert code == 3, output
    assert "b-fake.jpg" in output
    assert "COMPLETED" in output  # the good one still ran
    assert fake.calls == 1


def test_ac_a_10_runs_are_isolated(data_dir: Path, claude: StartClaude, receipt: Path) -> None:
    claude(then=answer())

    _process("--run", "a", str(receipt))
    _process("--run", "b", str(receipt))

    for run in ("a", "b"):
        assert rows(run_database(data_dir, run), "SELECT id FROM receipts") == [(1,)]
    main = data_dir / DATABASE_FILENAME
    assert rows(main, "SELECT count(*) FROM receipts") == [(0,)]
    assert rows(main, "SELECT count(*) FROM claude_calls") == [(2,)]  # R3: one ledger


# --- Claude unavailable or no key (exit 4) -----------------------------------------------------


def test_c1_a_named_run_never_migrates_the_main_database(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """Ruling C1: the ledger needs the main database at head; a ``--run`` only migrates its own
    database, so an old main one is refused, left at 0006 with its vectors."""
    main = data_dir / DATABASE_FILENAME
    main.unlink()
    command.upgrade(alembic_config(main), "0006")
    fake = claude(answer())

    code, output = _process("--run", "scratch", str(receipt))

    assert code == 1, output
    assert "not migrated" in output
    assert fake.calls == 0
    assert rows(main, "SELECT version_num FROM alembic_version") == [("0006",)]
    assert rows(main, "SELECT name FROM sqlite_master WHERE name = 'product_vectors'") == [
        ("product_vectors",)
    ]


def test_r3_the_budget_is_counted_in_the_main_database_whatever_the_run(
    data_dir: Path, claude: StartClaude, receipt: Path, copy_migrated: CopyMigrated
) -> None:
    """Review round 1, R3: the month's spend lives in ``DATA_DIR``'s database, so a new
    ``--run`` starts with the spend already made, not with an empty ledger."""
    main = create_engine(copy_migrated(data_dir / DATABASE_FILENAME))
    try:
        ledger = SqliteSpendLedger(main)
        now = datetime.now(UTC)
        reservation = ledger.reserve(
            now, "claude-opus-5-5", Decimal("5.00"), since=month_start(now), cap_usd=Decimal(10)
        )
        assert reservation is not None
        ledger.settle(
            reservation,
            ClaudeCall(
                at=now,
                model="claude-opus-5-5",
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("5.00"),
            ),
        )
    finally:
        main.dispose()
    fake = claude(answer())

    code, output = _process("--run", "other", str(receipt))

    assert code == 4, output
    assert "monthly Claude budget reached" in output
    assert "PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD" in output
    assert fake.calls == 0


def test_ce_tc_040_without_an_api_key_process_names_the_variable(
    data_dir: Path, receipt: Path
) -> None:
    """CE-TC-040: no ``ANTHROPIC_API_KEY`` (the session clears it): no request is made (a
    request to the real API would fail the loopback guard), exit 4 naming the variable."""
    code, output = _process("--run", "nokey", str(receipt))

    assert code == 4, output
    assert "ANTHROPIC_API_KEY" in output
    assert rows(run_database(data_dir, "nokey"), "SELECT status FROM receipts") == [("failed",)]


@pytest.mark.parametrize(
    "command",
    [("catalog", "list"), ("catalog", "todo"), ("prices", "backfill"), ("review", "list")],
    ids=["catalog-list", "catalog-todo", "prices-backfill", "review-list"],
)
def test_ce_tc_040_commands_without_a_model_need_no_key(
    data_dir: Path, copy_migrated: CopyMigrated, command: tuple[str, ...]
) -> None:
    copy_migrated(data_dir / DATABASE_FILENAME)

    result = invoke(*command)

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" not in result.output


def test_claude_unavailable_stops_the_batch(
    data_dir: Path, claude: StartClaude, tmp_path: Path
) -> None:
    claude(then=UNAVAILABLE)
    folder = tmp_path / "receipts"
    folder.mkdir()
    for index in range(2):
        photo(folder, f"{index}.png", index)

    code, _ = _process("--run", "stop", str(folder))

    assert code == 4
    assert rows(run_database(data_dir, "stop"), "SELECT count(*) FROM receipts") == [(1,)]


# --- usage errors and the main database ---------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [(), ("--run", "x", "missing.jpg"), ("--run", "../escape", "costco.png")],
    ids=["no-input", "missing-path", "bad-run-name"],
)
def test_ac_a_10_usage_errors_are_exit_2(
    data_dir: Path,
    claude: StartClaude,
    receipt: Path,
    args: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(receipt.parent)
    fake = claude(answer())

    code, output = _process(*args)

    assert code == 2, output
    assert fake.calls == 0


def test_the_main_database_must_be_migrated(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    (data_dir / DATABASE_FILENAME).unlink()
    fake = claude(answer())

    code, output = _process(str(receipt))

    assert code == 1, output
    assert "db upgrade" in output
    assert fake.calls == 0


def test_the_main_run_uses_the_main_database(
    data_dir: Path, claude: StartClaude, receipt: Path, copy_migrated: CopyMigrated
) -> None:
    copy_migrated(data_dir / DATABASE_FILENAME)
    claude(answer())

    code, output = _process(str(receipt))

    assert code == 0, output
    assert receipt_status(data_dir / "paper_trail.db", 1) == "completed"
    assert len(trace_files(data_dir, "main")) == 1
