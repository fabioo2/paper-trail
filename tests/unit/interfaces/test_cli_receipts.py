"""The `receipts` command group of the CLI (S1-AC-24, PLAN 3.8)."""

from typer.testing import CliRunner

from paper_trail.interfaces.cli.main import app as cli

runner = CliRunner()


def test_s1_ac_24_help_lists_the_receipts_group() -> None:
    """S1-AC-24: `paper-trail --help` lists `receipts`."""
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "receipts" in result.output


def test_s1_ac_24_receipts_group_has_import_and_show() -> None:
    """S1-AC-24: `paper-trail receipts --help` lists import and show."""
    result = runner.invoke(cli, ["receipts", "--help"])

    assert result.exit_code == 0, result.output
    assert "import" in result.output
    assert "show" in result.output


def test_s1_ac_24_import_offers_dry_run() -> None:
    """S1-AC-24: `receipts import` takes a FILE and a --dry-run flag."""
    result = runner.invoke(cli, ["receipts", "import", "--help"])

    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output
    assert "FILE" in result.output
