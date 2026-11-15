"""Catalog settings (barcode-lookup PR 1a, R11; PC-TC-77).

Pinned (signatures.md):
- ``catalog: CatalogSettings`` (``PAPER_TRAIL_CATALOG__*``), frozen: ``enabled: bool = True``
  (false sends coded lines the way of uncoded ones) and ``plu_path``, which ``Settings`` fills
  with ``<data_dir>/catalog/plu.csv`` unless it is set.
"""

from pathlib import Path

import pytest

from paper_trail.config import Settings


def test_the_catalog_is_on_and_reads_plu_csv_in_the_data_dir() -> None:
    settings = Settings()

    assert settings.catalog.enabled is True
    assert settings.catalog.plu_path == settings.data_dir / "catalog" / "plu.csv"


def test_the_plu_path_follows_the_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(tmp_path / "data"))

    assert Settings().catalog.plu_path == tmp_path / "data" / "catalog" / "plu.csv"


@pytest.mark.parametrize(
    ("variable", "value", "setting", "expected"),
    [
        ("ENABLED", "false", "enabled", False),
        ("ENABLED", "0", "enabled", False),
        ("ENABLED", "true", "enabled", True),
        ("ENABLED", "1", "enabled", True),
        ("ENABLED", "", "enabled", True),
        ("PLU_PATH", "/x/p.csv", "plu_path", Path("/x/p.csv")),
    ],
    ids=["false", "0", "true", "1", "empty-is-unset", "plu-path"],
)
def test_the_catalog_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, setting: str, expected: object
) -> None:
    monkeypatch.setenv(f"PAPER_TRAIL_CATALOG__{variable}", value)

    assert getattr(Settings().catalog, setting) == expected
