"""Domain entities and enums (S1-AC-02)."""

import dataclasses
import json
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum

import pytest

from paper_trail.domain import enums
from paper_trail.domain.errors import (
    CurrencyMismatchError,
    DomainValidationError,
    InvalidQuantityError,
)
from paper_trail.domain.models import LineItem, Receipt, ReceiptDraft, require_id
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode
from paper_trail.ports.search import HitKind
from tests.factories import line_item, price_observation, receipt, receipt_draft

ALL_ENUMS: list[type[StrEnum]] = [
    enums.Category,
    enums.LineKind,
    enums.Unit,
    enums.ReceiptStatus,
    enums.ProcessingStage,
    enums.JobKind,
    enums.JobStatus,
    enums.ReviewReason,
    enums.ReviewStatus,
    enums.AliasSource,
    enums.CorrectionKind,
    IssueCode,
    HitKind,
]

# PLAN 3.2: the members every later stage relies on.
EXPECTED_MEMBERS: dict[type[StrEnum], set[str]] = {
    enums.Category: {
        "PRODUCE", "MEAT_SEAFOOD", "DAIRY_EGGS", "BAKERY", "DELI_PREPARED", "PANTRY", "FROZEN",
        "BEVERAGES", "SNACKS_CANDY", "ALCOHOL", "HOUSEHOLD", "PERSONAL_CARE", "HEALTH_PHARMACY",
        "BABY", "PET", "ELECTRONICS", "CLOTHING", "HOME_GARDEN", "AUTOMOTIVE_FUEL", "RESTAURANT",
        "SERVICES", "OTHER",
    },
    enums.LineKind: {"ITEM", "DISCOUNT", "DEPOSIT", "FEE"},
    enums.Unit: {"EACH", "G", "KG", "ML", "L", "LB", "OZ"},
    enums.ReceiptStatus: {"PROCESSING", "NEEDS_REVIEW", "COMPLETED", "FAILED"},
    enums.ProcessingStage: {
        "QUEUED", "WAITING_FOR_GPU", "READING", "EXTRACTING", "VALIDATING", "MATCHING",
        "ENRICHING", "SAVING", "DONE", "NEEDS_REVIEW", "FAILED",
    },
    enums.JobKind: {"PROCESS_RECEIPT"},
    enums.JobStatus: {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"},
    enums.ReviewReason: {
        "VALIDATION_FAILED", "LOW_CONFIDENCE", "EXTRACTION_FAILED", "UNMATCHED_PRODUCT",
    },
    enums.ReviewStatus: {"OPEN", "RESOLVED", "DISMISSED"},
    enums.AliasSource: {"IMPORT", "EXTRACTED", "MATCHED", "USER_CORRECTION"},
    enums.CorrectionKind: {"PRODUCT_LINK", "CATEGORY", "FIELD"},
    IssueCode: {
        "LINE_MATH", "ITEMS_VS_SUBTOTAL", "SUBTOTAL_PLUS_TAX_VS_TOTAL", "ITEMS_PLUS_TAX_VS_TOTAL",
        "NEGATIVE_ITEM", "CURRENCY_MISMATCH", "EMPTY_RECEIPT",
        "MISSING_STORE", "MISSING_DATE",  # completeness, extraction accuracy (ADR-0017)
    },
    HitKind: {"LINE_ITEM", "PRODUCT"},
}  # fmt: skip


@pytest.mark.parametrize("enum", ALL_ENUMS, ids=lambda enum: enum.__name__)
def test_s1_ac_02_enum_members_are_the_planned_ones(enum: type[StrEnum]) -> None:
    """S1-AC-02 / PLAN 3.2: each enum has exactly the planned members."""
    assert {member.name for member in enum} == EXPECTED_MEMBERS[enum]


@pytest.mark.parametrize("enum", ALL_ENUMS, ids=lambda enum: enum.__name__)
def test_s1_ac_02_enums_serialise_to_their_lowercase_name(enum: type[StrEnum]) -> None:
    """S1-AC-02: every value is the member name in lowercase, also through json.dumps."""
    for member in enum:
        assert member.value == member.name.lower()
        assert str(member) == member.name.lower()
        assert json.loads(json.dumps(member)) == member.name.lower()
        assert enum(member.name.lower()) is member


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-1"), Decimal("-0.001")])
def test_s1_ac_02_line_item_rejects_non_positive_quantity(quantity: Decimal) -> None:
    """S1-AC-02: LineItem.quantity must be > 0."""
    with pytest.raises(InvalidQuantityError):
        line_item(quantity=quantity)


def test_s1_ac_02_line_item_accepts_fractional_quantity() -> None:
    """S1-AC-02: weighed goods have fractional quantities."""
    assert line_item(quantity=Decimal("0.452")).quantity == Decimal("0.452")


def test_s1_ac_02_invalid_quantity_is_a_validation_error() -> None:
    """Pinned: InvalidQuantityError is a DomainValidationError (HTTP 422)."""
    assert issubclass(InvalidQuantityError, DomainValidationError)


@pytest.mark.parametrize("last4", ["123", "12345", "abcd", "12 4", "١٢٣٤"])
def test_s1_ac_02_draft_rejects_a_malformed_payment_last4(last4: str) -> None:
    """Pinned (PLAN 3.2 ^\\d{4}$): ASCII digits only, exactly four."""
    with pytest.raises(DomainValidationError):
        receipt_draft(payment_last4=last4)


def test_s1_ac_02_draft_product_names_default_to_empty() -> None:
    """ReceiptDraft.product_names defaults to an empty mapping (only the import sets it)."""
    draft = ReceiptDraft(
        store_name="Costco",
        purchased_on=None,
        currency="CAD",
        subtotal=None,
        tax=None,
        total=receipt_draft().total,
        items=(),
        payment_last4=None,
        ocr_text=None,
        image_path=None,
    )

    assert dict(draft.product_names) == {}


@pytest.mark.parametrize("model", [line_item, receipt_draft, receipt])
def test_s1_ac_02_entities_are_frozen_with_slots(model: Callable[[], object]) -> None:
    """PLAN 1.2: domain types are frozen dataclasses with slots."""
    instance = model()

    assert dataclasses.is_dataclass(instance)
    assert not hasattr(instance, "__dict__")
    field = dataclasses.fields(instance)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field, None)


def test_s1_ac_02_collections_are_tuples() -> None:
    """PLAN 3.2: collections on entities are tuples."""
    assert isinstance(receipt().items, tuple)
    assert isinstance(receipt_draft().items, tuple)
    assert all(isinstance(item, LineItem) for item in receipt().items)
    assert isinstance(receipt(), Receipt)


@pytest.mark.parametrize("quantity", [Decimal("NaN"), Decimal("Infinity")])
def test_s1_ac_02_line_item_rejects_non_finite_quantity(quantity: Decimal) -> None:
    """S1-AC-02: a quantity must be a positive, finite number."""
    with pytest.raises(InvalidQuantityError):
        line_item(quantity=quantity)


def test_s1_ac_02_a_stored_receipt_is_in_one_currency() -> None:
    """A Receipt cannot be stored half in another currency (the schema keeps one per receipt)."""
    usd_line = line_item(unit_price=None, line_total=Money(899, "USD"))

    with pytest.raises(CurrencyMismatchError):
        receipt(items=(usd_line,))
    with pytest.raises(CurrencyMismatchError):
        receipt(tax=Money(78, "USD"))


def test_s1_ac_02_a_price_observation_is_in_one_currency() -> None:
    """Unit price and line total of an observation share its single currency column."""
    with pytest.raises(CurrencyMismatchError):
        price_observation(product_id=1, receipt_id=1, line_item_id=1, line_total=Money(899, "USD"))


def test_require_id_returns_the_id_of_a_stored_entity() -> None:
    """require_id passes a stored id through and refuses an unsaved entity's None."""
    assert require_id(7) == 7
    with pytest.raises(ValueError, match="stored entity"):
        require_id(None)
