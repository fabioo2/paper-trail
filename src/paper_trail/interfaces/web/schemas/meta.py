"""Reference data the client shows in pickers and filters."""

from collections.abc import Mapping

from pydantic import BaseModel

from paper_trail.domain.enums import Category

# The one place categories get their display names.
CATEGORY_LABELS: Mapping[Category, str] = {
    Category.PRODUCE: "Produce",
    Category.MEAT_SEAFOOD: "Meat & seafood",
    Category.DAIRY_EGGS: "Dairy & eggs",
    Category.BAKERY: "Bakery",
    Category.DELI_PREPARED: "Deli & prepared",
    Category.PANTRY: "Pantry",
    Category.FROZEN: "Frozen",
    Category.BEVERAGES: "Beverages",
    Category.SNACKS_CANDY: "Snacks & candy",
    Category.ALCOHOL: "Alcohol",
    Category.HOUSEHOLD: "Household",
    Category.PERSONAL_CARE: "Personal care",
    Category.HEALTH_PHARMACY: "Health & pharmacy",
    Category.BABY: "Baby",
    Category.PET: "Pet",
    Category.ELECTRONICS: "Electronics",
    Category.CLOTHING: "Clothing",
    Category.HOME_GARDEN: "Home & garden",
    Category.AUTOMOTIVE_FUEL: "Automotive & fuel",
    Category.RESTAURANT: "Restaurant",
    Category.SERVICES: "Services",
    Category.OTHER: "Other",
}


class CategoryOut(BaseModel):
    value: Category
    label: str


def categories() -> list[CategoryOut]:
    """Every category in enum order with its label."""
    return [CategoryOut(value=category, label=CATEGORY_LABELS[category]) for category in Category]
