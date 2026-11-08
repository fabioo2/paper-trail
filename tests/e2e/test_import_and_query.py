"""End to end: `paper-trail receipts import` then the HTTP API (S1-AC-24, PLAN 3.8).

The CLI runs in-process through Typer's runner against a temporary data dir (the real process
boundary is ``test_cli_processes.py``); the API is the fully wired app on the same database.

Pinned (docs/tests/stage1.md): ``receipts import`` needs a migrated database (otherwise exit 1
with a hint to run ``db upgrade``); it prints ``receipt <id>: <status>`` and one line per
arithmetic issue; validation errors go to stderr with exit 2; ``--dry-run`` prints
``dry run: <status it would get>`` and the issues; ``receipts show`` prints only the JSON
document (exit 1 and a message on stderr for an unknown id).
"""

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import Result

from paper_trail.bootstrap import build_container, dispose
from paper_trail.config import DATABASE_FILENAME
from paper_trail.interfaces.web.app import create_app
from tests.e2e.agent_cli import configure_cli, invoke
from tests.support import REPO_ROOT, CopyMigrated, MakeSettings

COSTCO_SIMPLE = REPO_ROOT / "tests" / "data" / "receipts" / "costco_simple.json"


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def migrated(data_dir: Path, copy_migrated: CopyMigrated) -> Path:
    copy_migrated(data_dir / DATABASE_FILENAME)
    return data_dir


@pytest.fixture
def api(data_dir: Path, make_settings: MakeSettings) -> Iterator[TestClient]:
    container = build_container(make_settings(data_dir=data_dir))
    try:
        with TestClient(create_app(container), raise_server_exceptions=False) as client:
            yield client
    finally:
        dispose(container)


def _import(path: Path, *flags: str) -> tuple[int, str, str]:
    result = invoke("receipts", "import", str(path), *flags)
    _assert_no_crash(result)
    return result.exit_code, result.stdout, result.stderr


def _assert_no_crash(result: Result) -> None:
    """The in-process form of "no traceback": the command ended with an exit code, not an
    exception escaping it."""
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "import.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(COSTCO_SIMPLE.read_text(encoding="utf-8"))
    return data


def _row_count(data_dir: Path) -> int:
    with closing(sqlite3.connect(data_dir / "paper_trail.db")) as connection:
        count: int = connection.execute("SELECT count(*) FROM receipts").fetchone()[0]
    return count


def test_s1_ac_24_import_then_list_over_http(migrated: Path, api: TestClient) -> None:
    """S1-AC-24: the imported sample shows up in GET /api/receipts and its detail adds up."""
    code, stdout, stderr = _import(COSTCO_SIMPLE)

    assert code == 0, stderr
    match = re.search(r"receipt (\d+): completed", stdout)
    assert match, stdout
    receipt_id = int(match.group(1))
    (summary,) = api.get("/api/receipts").json()["items"]
    assert summary["id"] == receipt_id
    assert summary["store_name"] == "COSTCO WHOLESALE #1234"
    assert summary["total"] == {"amount_cents": 3292, "currency": "CAD"}
    assert summary["item_count"] == 5
    detail = api.get(f"/api/receipts/{receipt_id}").json()
    assert detail["arithmetic"] == {"ok": True, "issues": []}
    assert [item["quantity"] for item in detail["items"]] == ["1", "2", "1.36", "1", "1"]
    assert sum(item["product_id"] is not None for item in detail["items"]) == 4


def test_s1_ac_24_inconsistent_import_is_recorded_for_review(
    migrated: Path, api: TestClient, tmp_path: Path
) -> None:
    """S1-AC-24 / PLAN 3.8: exit 0 even when the receipt needs review; issues are printed."""
    data = _sample()
    data["total_cents"] = 3300

    code, stdout, stderr = _import(_write(tmp_path, data))

    assert code == 0, stderr
    assert re.search(r"receipt \d+: needs_review", stdout), stdout
    assert "32.92" in stdout and "33.00" in stdout, "the issue message is printed"
    (summary,) = api.get("/api/receipts?status=needs_review").json()["items"]
    assert summary["total"]["amount_cents"] == 3300


def test_s1_ac_24_dry_run_writes_nothing(migrated: Path, data_dir: Path, tmp_path: Path) -> None:
    """S1-AC-24: --dry-run validates and reports, and the database stays empty."""
    data = _sample()
    data["total_cents"] = 3300

    code, stdout, stderr = _import(_write(tmp_path, data), "--dry-run")

    assert code == 0, stderr
    assert "dry run: needs_review" in stdout
    assert "32.92" in stdout and "33.00" in stdout
    assert _row_count(data_dir) == 0


def test_s1_ac_24_dry_run_of_a_consistent_file(migrated: Path, data_dir: Path) -> None:
    """S1-AC-24: a consistent file reports the status it would get, still writing nothing."""
    code, stdout, _ = _import(COSTCO_SIMPLE, "--dry-run")

    assert code == 0
    assert "dry run: completed" in stdout
    assert _row_count(data_dir) == 0


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda data: data.update(cashier="Sam"), "cashier"),
        (lambda data: data["items"][0].update(barcode="0123"), "barcode"),
        (lambda data: data.update(total_cents=32.92), "total_cents"),
        (lambda data: data.update(total_cents="3292"), "total_cents"),
        (lambda data: data["items"][1].update(line_total_cents=6.58), "line_total_cents"),
    ],
)
def test_s1_ac_24_invalid_file_exits_2_naming_the_field(
    migrated: Path,
    data_dir: Path,
    tmp_path: Path,
    change: Callable[[dict[str, Any]], None],
    field: str,
) -> None:
    """S1-AC-24: an unknown field or a non-integer cents value exits 2 and names the field."""
    data = _sample()
    change(data)

    code, stdout, stderr = _import(_write(tmp_path, data))

    assert code == 2
    assert field in stderr
    assert "Traceback" not in stderr + stdout
    assert _row_count(data_dir) == 0


def test_s1_ac_24_malformed_json_exits_2(migrated: Path, tmp_path: Path) -> None:
    """Pinned: a file that is not JSON is a validation error too (exit 2, no traceback)."""
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")

    code, stdout, stderr = _import(path)

    assert code == 2
    assert stderr.strip()
    assert "Traceback" not in stderr + stdout


def test_s1_ac_24_missing_file_exits_2(migrated: Path, tmp_path: Path) -> None:
    """Pinned: a path that does not exist is a usage error (exit 2)."""
    code, _, stderr = _import(tmp_path / "nope.json")

    assert code == 2
    assert "Traceback" not in stderr


def test_s1_ac_24_import_needs_a_migrated_database(data_dir: Path) -> None:
    """Pinned: without `db upgrade` the import exits 1 and says how to fix it, no traceback."""
    code, stdout, stderr = _import(COSTCO_SIMPLE)

    assert code == 1
    assert "db upgrade" in stderr
    assert "Traceback" not in stderr + stdout


def test_s1_ac_24_show_prints_the_api_detail(migrated: Path, api: TestClient) -> None:
    """PLAN 3.8: `receipts show ID` prints the receipt as JSON, the same shape as the API."""
    _, stdout, _ = _import(COSTCO_SIMPLE)
    match = re.search(r"receipt (\d+):", stdout)
    assert match, stdout
    receipt_id = match.group(1)

    shown = invoke("receipts", "show", receipt_id)

    assert shown.exit_code == 0, shown.stderr
    assert json.loads(shown.stdout) == api.get(f"/api/receipts/{receipt_id}").json()


def test_s1_ac_24_show_unknown_id_exits_1(migrated: Path) -> None:
    """Pinned: an unknown id prints a message on stderr and exits 1."""
    shown = invoke("receipts", "show", "999")

    _assert_no_crash(shown)
    assert shown.exit_code == 1
    assert "999" in shown.stderr
    assert shown.stdout == ""
