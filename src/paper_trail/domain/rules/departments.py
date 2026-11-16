"""A printed department heading as a category hint, per store (claude-extraction plan EC
section, E1).

Only stores with a map get a hint (Metro at first); an unknown heading, or a store without a
map, gives none. Headings are compared trimmed and case-insensitively.
"""

from paper_trail.domain.enums import Category

DEPARTMENT_CATEGORIES: dict[str, dict[str, Category]] = {
    "metro": {
        "PRODUCE": Category.PRODUCE,
        "HBC": Category.PERSONAL_CARE,
    },
}


def department_category(store_key: str, department: str | None) -> Category | None:
    """The category ``department`` names at the store ``store_key``
    (``normalize_store_name``), or None."""
    if department is None:
        return None
    return DEPARTMENT_CATEGORIES.get(store_key, {}).get(department.strip().upper())
