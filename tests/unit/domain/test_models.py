"""Domain entities and enums (S1-AC-02)."""

import dataclasses
import json
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain import enums
from paper_trail.domain.enums import LineKind
from paper_trail.domain.errors import (
    CurrencyMismatchError,
    DomainValidationError,
    InvalidQuantityError,
)
from paper_trail.domain.models import LineItem, Receipt, ReceiptDraft, require_id
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode
from paper_trail.ports.search import HitKind
from tests.factories import (
    CHOC,
    KIWI,
    PRINTED_LINKS,
    cad,
    discount_line,
    item_line,
    line_item,
    price_observation,
    printed_lines,
    receipt,
    receipt_draft,
    simple_lines,
)

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


# --- discount links at the receipt boundary (P4 LINKS_AT_THE_BOUNDARY: R4, R6, A3) -----------


def _links_to_kind(kind: LineKind) -> tuple[LineItem, ...]:
    return (
        line_item(kind=kind, raw_text="TARGET", unit_price=None, line_total=cad(100)),
        item_line(1, CHOC, 1699),
        discount_line(2, "TPD/1362394", -50, 0),
    )


def _boundary_error(*positions: int) -> tuple[str, ...]:
    """What a refused link's message names: the field and the 1-based lines (Q25)."""
    return ("applies_to_position", *(f"line {position}" for position in positions))


LINKS_AT_THE_BOUNDARY: list[object] = [
    *[
        pytest.param(
            lambda kind=kind: (
                item_line(0, CHOC, 1699),
                line_item(position=1, kind=kind, applies_to_position=0),
            ),
            _boundary_error(),
            id=f"TC-37-link-on-{kind}",
        )
        for kind in (LineKind.ITEM, LineKind.DEPOSIT, LineKind.FEE)
    ],
    pytest.param(
        lambda: (item_line(0, CHOC, 1699), discount_line(1, "TPD/1362394", -400, 1)),
        _boundary_error(),
        id="TC-38-own-position",
    ),
    pytest.param(
        lambda: (item_line(0, CHOC, 1699), discount_line(1, "TPD/1362394", -400, -1)),
        _boundary_error(),
        id="TC-39-negative",
    ),
    pytest.param(
        lambda: (item_line(0, CHOC, 1699), discount_line(1, "TPD/1362394", -400, 5)),
        _boundary_error(),
        id="TC-39-not-a-line",
    ),
    *[
        pytest.param(
            lambda kind=kind: _links_to_kind(kind),
            _boundary_error(3, 1),
            id=f"TC-41-target-is-{kind}",
        )
        for kind in (LineKind.DISCOUNT, LineKind.DEPOSIT, LineKind.FEE)
    ],
    pytest.param(
        lambda: (
            item_line(0, CHOC, 1699),
            discount_line(1, "TPD/1362394", -400, 0),
            discount_line(2, "TPD/1362394", -400, 0),
        ),
        _boundary_error(3, 1),
        id="TC-42-one-item-twice",
    ),
    pytest.param(
        lambda: (item_line(0, CHOC, 1699), discount_line(1, "TPD/1362394", 0, 0)),
        _boundary_error(2, 1),
        id="TC-111-zero",
    ),
    pytest.param(
        lambda: (item_line(0, CHOC, 1699), discount_line(1, "TPD/1362394", 400, 0)),
        _boundary_error(2, 1),
        id="TC-111-positive",
    ),
    pytest.param(
        lambda: (item_line(0, KIWI, 399), discount_line(1, "TPD/1543210", -400, 0)),
        _boundary_error(2, 1),
        id="TC-112-exceeds-item",
    ),
    pytest.param(
        lambda: (item_line(0, KIWI, 399), discount_line(1, "TPD/1543210", -399, 0)),
        None,
        id="TC-112-equal-accepted",
    ),
    pytest.param(
        lambda: (
            item_line(0, CHOC, 1699),
            discount_line(1, "TPD/1362394", -400, 3),
            item_line(2, CHOC, 1699),
            item_line(3, KIWI, 1599),
        ),
        None,
        id="TC-113-later-item",
    ),
    pytest.param(
        lambda: (discount_line(0, "TPD/1362394", -400, 1), item_line(1, KIWI, 1599)),
        None,
        id="TC-113-other-code",
    ),
    pytest.param(lambda: simple_lines(link=True), None, id="TC-43-simple"),
    pytest.param(lambda: printed_lines(PRINTED_LINKS), None, id="TC-43-printed"),
]


@pytest.mark.parametrize("build", [receipt_draft, receipt], ids=["draft", "receipt"])
@pytest.mark.parametrize(("lines", "error"), LINKS_AT_THE_BOUNDARY)
def test_discount_links_at_the_receipt_boundary(
    build: Callable[..., ReceiptDraft | Receipt],
    lines: Callable[[], tuple[LineItem, ...]],
    error: tuple[str, ...] | None,
) -> None:
    if error is None:
        items = lines()
        assert build(items=items).items == items
        return
    with pytest.raises(DomainValidationError) as raised:
        build(items=lines())
    message = str(raised.value)
    assert [part for part in error if part not in message] == []


# --- paid prices (P5 PAID_PRICES, P5b INVALID_DISCOUNTS: R5, DA-1) ---------------------------

PAID_PRICES = [
    # quantity, unit price, line total, discount -> unit discount, paid, paid unit price, on sale
    pytest.param("1", 899, 899, 0, (0, 899, 899, False), id="TC-44-no-discount"),
    pytest.param("2", 329, 658, 0, (0, 658, 329, False), id="TC-44-paid-is-the-line-total"),
    pytest.param("1", 1699, 1699, -400, (-400, 1299, 1299, True), id="TC-45"),
    pytest.param("2", 329, 658, -100, (-50, 558, 279, True), id="TC-47-two"),
    pytest.param("3", 333, 999, -100, (-33, 899, 300, True), id="TC-47-three"),
    pytest.param("2", 329, 658, -5, (-3, 653, 327, True), id="TC-48-half-up"),
    pytest.param("1.36", 150, 204, -50, (-37, 154, 113, True), id="TC-49-weighed"),
    pytest.param("1", 399, 399, -399, (-399, 0, 0, True), id="TC-50-paid-zero"),
]


@pytest.mark.parametrize(("quantity", "unit", "total", "discount", "expected"), PAID_PRICES)
def test_paid_prices(
    quantity: str,
    unit: int,
    total: int,
    discount: int,
    expected: tuple[int, int, int, bool],
) -> None:
    observation = price_observation(
        product_id=1,
        receipt_id=1,
        line_item_id=1,
        quantity=Decimal(quantity),
        unit_price=cad(unit),
        line_total=cad(total),
        discount=cad(discount),
    )

    unit_discount, paid, paid_unit, on_sale = expected
    assert (
        observation.unit_price,
        observation.line_total,
        observation.discount,
        observation.unit_discount,
        observation.paid,
        observation.paid_unit_price,
        observation.on_sale,
    ) == (
        cad(unit),
        cad(total),
        cad(discount),
        cad(unit_discount),
        cad(paid),
        cad(paid_unit),
        on_sale,
    )


INVALID_DISCOUNTS = [
    pytest.param(899, cad(1), DomainValidationError, "discount", id="TC-51-positive"),
    pytest.param(399, cad(-400), DomainValidationError, None, id="TC-52-paid-below-zero"),
    pytest.param(899, Money(-100, "USD"), CurrencyMismatchError, None, id="TC-53-currency"),
]


@pytest.mark.parametrize(("total", "discount", "error", "names"), INVALID_DISCOUNTS)
def test_invalid_discounts(
    total: int, discount: Money, error: type[DomainValidationError], names: str | None
) -> None:
    with pytest.raises(error, match=names):
        price_observation(
            product_id=1,
            receipt_id=1,
            line_item_id=1,
            unit_price=cad(total),
            line_total=cad(total),
            discount=discount,
        )


@given(
    quantity=st.decimals(min_value=Decimal("0.001"), max_value=1000, places=3),
    total=st.integers(min_value=0, max_value=10**7),
    share=st.fractions(min_value=0, max_value=1),
)
def test_tc_54_paid_is_the_line_total_plus_the_discount(
    quantity: Decimal, total: int, share: Fraction
) -> None:
    discount = -int(total * share)
    observation = price_observation(
        product_id=1,
        receipt_id=1,
        line_item_id=1,
        quantity=quantity,
        unit_price=cad(total).divided_by(quantity),
        line_total=cad(total),
        discount=cad(discount),
    )

    assert observation.paid == cad(total + discount)
    assert observation.paid.amount_cents >= 0
    assert observation.on_sale is (discount < 0)
    assert observation.unit_discount == cad(discount).divided_by(quantity)
    assert observation.unit_discount.amount_cents <= 0
