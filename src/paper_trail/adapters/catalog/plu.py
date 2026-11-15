"""The PLU table: ``<data_dir>/catalog/plu.csv`` (``code,name,organic``), read once into memory.

A missing, empty or unreadable file, or one without a ``code`` or ``name`` column, is a miss
for every lookup, with one ``catalog.missing`` warning per catalog naming the path and the
reason. A file with only its header misses silently. The ``organic`` column is not used: an
organic code is the conventional one with a 9 in front.
"""

import csv
from pathlib import Path

import structlog

from paper_trail.domain.enums import Category, CodeKind
from paper_trail.domain.rules.item_codes import ItemCode
from paper_trail.ports.catalog import CatalogProduct

log = structlog.get_logger(__name__)

ORGANIC = "organic"


class PluCatalog:
    """Implements ``paper_trail.ports.catalog.ProductCatalog`` for PLU codes only: a hit is
    source ``plu``, key the lookup key (``4046`` for an organic ``94046``), category produce;
    an organic code's name is "Organic " + the table's name, with the tag ``organic``."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._names: dict[str, str] | None = None

    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None:
        if code.kind is not CodeKind.PLU or not code.keys:
            return None
        key = code.keys[0]
        name = self._table().get(key)
        if name is None:
            return None
        return CatalogProduct(
            source="plu",
            key=key,
            product_id=None,
            name=f"Organic {name}" if code.organic else name,
            brand=None,
            size_value=None,
            size_unit=None,
            category=Category.PRODUCE,
            tags=(ORGANIC,) if code.organic else (),
        )

    def _table(self) -> dict[str, str]:
        if self._names is None:
            names, problem = _read(self._path)
            if problem is not None:
                log.warning("catalog.missing", path=str(self._path), reason=problem)
            self._names = names
        return self._names


def _read(path: Path) -> tuple[dict[str, str], str | None]:
    """The table's names by code, and why it could not be read (None when it could)."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            rows = csv.DictReader(file)
            columns = rows.fieldnames
            if not columns:
                return {}, "no header"
            for column in ("code", "name"):
                if column not in columns:
                    return {}, f"no {column} column"
            return {
                row["code"].strip(): row["name"].strip()
                for row in rows
                if row.get("code") and row.get("name")
            }, None
    except FileNotFoundError:
        return {}, "no such file"
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        return {}, f"unreadable: {type(error).__name__}"
