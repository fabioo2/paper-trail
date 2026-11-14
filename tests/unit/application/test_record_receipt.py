"""RecordReceipt with in-memory fakes (S1-AC-19, S1-AC-20, S1-AC-21).

Pinned (docs/tests/stage1.md):
- a line with an import product name ``name`` is linked through the global alias
  ``normalize_alias(name)`` (created with source IMPORT on first sight, with a new Product whose
  canonical_name is ``name`` and whose category is the line's); its raw text is also remembered
  as a store-specific IMPORT alias ``normalize_alias(raw_text)`` for ``store_key``;
- a price observation's unit_price is the line's unit_price, else line_total / quantity rounded
  half up to the cent; observed_on is purchased_on;
- review details are ``{"issues": [{"code", "message", "expected", "actual", "position"}]}``
  with Money as ``{"amount_cents", "currency"}``.
"""

from dataclasses import replace
from decimal import Decimal

import pytest
from structlog.testing import capture_logs

from paper_trail.application.receipts import RecordReceipt, RecordReceiptResult
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    LineKind,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.models import PriceObservation, Receipt, ReceiptDraft
from paper_trail.domain.paging import PageRequest
from paper_trail.domain.rules.arithmetic import IssueCode, Tolerances
from paper_trail.domain.rules.discounts import DiscountAttribution, DiscountLink
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import (
    CHOC,
    MASKED_TEST_PAN,
    PURCHASED_ON,
    TEST_PAN,
    cad,
    discount_line,
    draft_with,
    item_line,
    line_item,
    price_observation,
    receipt_draft,
    simple_draft,
)
from tests.fakes.clock import FakeClock
from tests.fakes.repositories import FakePriceRepository
from tests.fakes.state import FakeState
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.support import require_id

EGGS = "Kirkland Signature Organic Eggs 24 ct"
MILK = "Kirkland 2% Milk 4 L"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def uow_factory(database: FakeDatabase, clock: FakeClock) -> UnitOfWorkFactory:
    return fake_uow_factory(database, clock)


@pytest.fixture
def record(uow_factory: UnitOfWorkFactory, clock: FakeClock) -> RecordReceipt:
    return RecordReceipt(uow_factory, clock, Tolerances())


def _named(draft: ReceiptDraft, names: dict[int, str]) -> ReceiptDraft:
    return replace(draft, product_names=names)


def _state(database: FakeDatabase) -> FakeState:
    return database.state


def _inconsistent_draft() -> ReceiptDraft:
    """Line 2 says 2 x 3.29 = 6.85 (expected 6.58), and the subtotal no longer adds up."""
    eggs, milk = receipt_draft().items
    return receipt_draft(items=(eggs, replace(milk, line_total=cad(685))))


# --- consistent drafts (S1-AC-19) -----------------------------------------------------------


def test_s1_ac_19_consistent_draft_is_completed(
    record: RecordReceipt, database: FakeDatabase, clock: FakeClock
) -> None:
    """S1-AC-19: a consistent draft is stored as COMPLETED/DONE with its arithmetic report."""
    result = record(receipt_draft())

    assert isinstance(result, RecordReceiptResult)
    saved = result.receipt
    assert saved.id is not None
    assert (saved.status, saved.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert result.arithmetic.ok
    assert result.review is None
    assert result.masked_pans == 0
    assert _state(database).receipts == {saved.id: saved}
    assert saved.created_at == saved.updated_at == clock.now()
    assert database.commits == 1


def test_s1_ac_19_copies_the_draft_fields(record: RecordReceipt) -> None:
    """S1-AC-19: every draft field lands on the receipt; store_key is the normalized name."""
    draft = receipt_draft(image_path="2026/11/abc.jpg")

    saved = record(draft).receipt

    assert saved.store_name == draft.store_name
    assert saved.store_key == "costco"
    assert (saved.purchased_on, saved.currency) == (draft.purchased_on, draft.currency)
    assert (saved.subtotal, saved.tax, saved.total) == (draft.subtotal, draft.tax, draft.total)
    assert (saved.payment_last4, saved.ocr_text, saved.image_path) == (
        draft.payment_last4,
        draft.ocr_text,
        draft.image_path,
    )
    assert [replace(item, id=None) for item in saved.items] == list(draft.items)
    assert all(item.id is not None for item in saved.items)


def test_s1_ac_19_links_named_lines_creating_products_and_import_aliases(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-19: first sight of a product name creates the product and its IMPORT aliases."""
    result = record(_named(receipt_draft(), {0: EGGS, 1: MILK}))

    state = _state(database)
    eggs_line, milk_line = result.receipt.items
    products = {product.canonical_name: product for product in state.products.values()}
    assert set(products) == {EGGS, MILK}
    assert eggs_line.product_id == products[EGGS].id
    assert milk_line.product_id == products[MILK].id
    assert products[MILK].category is Category.DAIRY_EGGS
    aliases = {(alias.alias, alias.store_key): alias for alias in state.aliases.values()}
    assert aliases.keys() == {
        (normalize_alias(EGGS), None),
        (normalize_alias(MILK), None),
        (normalize_alias("KS ORG EGGS 24CT"), "costco"),
        (normalize_alias("KS 2% MILK 4L"), "costco"),
    }
    assert {alias.source for alias in aliases.values()} == {AliasSource.IMPORT}
    assert aliases[normalize_alias(EGGS), None].product_id == products[EGGS].id
    assert aliases[normalize_alias("KS 2% MILK 4L"), "costco"].product_id == products[MILK].id


def test_s1_ac_19_unnamed_lines_stay_unlinked(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-19: only lines with a product name are linked."""
    result = record(_named(receipt_draft(), {1: MILK}))

    assert [item.product_id is None for item in result.receipt.items] == [True, False]
    assert len(_state(database).products) == 1


def test_s1_ac_19_second_sight_reuses_the_product_across_stores(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-19: the same product name from another store's raw text links to the same product."""
    first = record(_named(receipt_draft(), {0: EGGS}))
    walmart_line = line_item(raw_text="KIRKLAND ORGANIC EGG 24", description="Organic eggs",
                             unit_price=cad(949), line_total=cad(949))  # fmt: skip
    walmart = receipt_draft(store_name="Walmart Supercentre 3150", items=(walmart_line,),
                            subtotal=cad(949), tax=None, total=cad(949))  # fmt: skip

    second = record(_named(walmart, {0: " kirkland signature ORGANIC eggs 24 CT "}))

    state = _state(database)
    assert len(state.products) == 1
    product_id = first.receipt.items[0].product_id
    assert product_id is not None
    assert second.receipt.items[0].product_id == product_id
    assert (normalize_alias("KIRKLAND ORGANIC EGG 24"), "walmart") in {
        (alias.alias, alias.store_key) for alias in state.aliases.values()
    }
    history = sorted(state.prices.values(), key=lambda row: require_id(row.id))
    assert [(row.product_id, row.store_key, row.unit_price) for row in history] == [
        (product_id, "costco", cad(899)),
        (product_id, "walmart", cad(949)),
    ]


def test_s1_ac_19_one_price_observation_per_linked_item_line(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-19: each linked ITEM line gets one observation; unlinked and non-ITEM lines none."""
    eggs, milk = receipt_draft().items
    deposit = line_item(
        position=2, kind=LineKind.DEPOSIT, raw_text="DEPOSIT", description="Deposit",
        unit_price=None, line_total=cad(10),
    )  # fmt: skip
    discount = line_item(
        position=3, kind=LineKind.DISCOUNT, raw_text="TPD", description="Savings",
        unit_price=None, line_total=cad(-10),
    )  # fmt: skip
    draft = receipt_draft(items=(eggs, milk, deposit, discount))

    result = record(_named(draft, {0: EGGS, 2: "Deposit", 3: "Savings"}))

    (observation,) = _state(database).prices.values()
    saved_eggs = result.receipt.items[0]
    assert observation.receipt_id == result.receipt.id
    assert observation.line_item_id == saved_eggs.id
    assert observation.product_id == saved_eggs.product_id
    assert observation.store_key == "costco"
    assert observation.observed_on == draft.purchased_on
    assert (observation.quantity, observation.unit_price, observation.line_total) == (
        Decimal("1"),
        cad(899),
        cad(899),
    )


def test_s1_ac_19_unit_price_is_derived_when_the_line_has_none(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """Pinned: no printed unit price -> line_total / quantity, rounded half up (999/2 -> 500)."""
    line = line_item(quantity=Decimal("2"), unit_price=None, line_total=cad(999))
    draft = receipt_draft(items=(line,), subtotal=cad(999), tax=None, total=cad(999))

    record(_named(draft, {0: EGGS}))

    (observation,) = _state(database).prices.values()
    assert observation.unit_price == cad(500)
    assert observation.quantity == Decimal("2")


def test_s1_ac_19_no_observations_without_a_purchase_date(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-19 / PLAN 3.6: prices need a date; lines are still linked."""
    result = record(_named(receipt_draft(purchased_on=None), {0: EGGS}))

    assert result.receipt.items[0].product_id is not None
    assert _state(database).prices == {}


# --- inconsistent drafts (S1-AC-20) ---------------------------------------------------------


def test_s1_ac_20_inconsistent_draft_needs_review(
    record: RecordReceipt, database: FakeDatabase, clock: FakeClock
) -> None:
    """S1-AC-20: NEEDS_REVIEW/NEEDS_REVIEW and exactly one OPEN VALIDATION_FAILED review item."""
    result = record(_named(_inconsistent_draft(), {0: EGGS, 1: MILK}))

    saved = result.receipt
    assert (saved.status, saved.stage) == (ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW)
    assert not result.arithmetic.ok
    (review,) = _state(database).reviews.values()
    assert result.review == review
    assert review.receipt_id == saved.id
    assert review.reason is ReviewReason.VALIDATION_FAILED
    assert review.status is ReviewStatus.OPEN
    assert review.created_at == clock.now()
    assert _state(database).prices == {}, "no prices from a receipt that does not add up"
    assert all(item.product_id is not None for item in saved.items), "lines are still linked"


def test_s1_ac_20_review_details_list_the_issues(record: RecordReceipt) -> None:
    """S1-AC-20: details.issues mirrors the arithmetic report, issue by issue."""
    result = record(_inconsistent_draft())

    assert result.review is not None
    issues = result.review.details["issues"]
    assert isinstance(issues, list)
    assert issues == [
        {
            "code": issue.code.value,
            "message": issue.message,
            "expected": None
            if issue.expected is None
            else {"amount_cents": issue.expected.amount_cents, "currency": issue.expected.currency},
            "actual": None
            if issue.actual is None
            else {"amount_cents": issue.actual.amount_cents, "currency": issue.actual.currency},
            "position": issue.position,
        }
        for issue in result.arithmetic.issues
    ]
    assert {issue["code"] for issue in issues if isinstance(issue, dict)} >= {
        IssueCode.LINE_MATH.value,
        IssueCode.ITEMS_VS_SUBTOTAL.value,
    }


def test_s1_ac_20_tolerances_are_injected(uow_factory: UnitOfWorkFactory, clock: FakeClock) -> None:
    """S1-AC-20: the configured tolerances decide what is inconsistent."""
    draft = receipt_draft(total=cad(1636))
    lenient = RecordReceipt(uow_factory, clock, Tolerances(per_line_cents=1, total_cents=1))

    assert lenient(draft).receipt.status is ReceiptStatus.COMPLETED


# --- masking (S1-AC-21) ---------------------------------------------------------------------


def _with_pans(draft: ReceiptDraft) -> ReceiptDraft:
    first, second = draft.items
    return replace(
        draft,
        ocr_text=f"VISA {TEST_PAN}\nTHANK YOU",
        items=(
            replace(first, raw_text=f"{first.raw_text} {TEST_PAN}"),
            replace(second, description=f"{second.description} {TEST_PAN}"),
        ),
    )


def test_s1_ac_21_masks_pans_before_storing(record: RecordReceipt, database: FakeDatabase) -> None:
    """S1-AC-21: ocr_text, raw_text and description are masked; masked_pans counts them."""
    result = record(_with_pans(receipt_draft()))

    (stored,) = _state(database).receipts.values()
    assert stored == result.receipt
    assert stored.ocr_text == f"VISA {MASKED_TEST_PAN}\nTHANK YOU"
    assert stored.items[0].raw_text == f"KS ORG EGGS 24CT {MASKED_TEST_PAN}"
    assert stored.items[1].description == f"Kirkland 2% Milk 4 L {MASKED_TEST_PAN}"
    assert result.masked_pans == 3


def test_s1_ac_21_aliases_are_made_from_masked_text(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-21: the store alias derived from raw_text never sees the card number."""
    record(_named(_with_pans(receipt_draft()), {0: EGGS}))

    store_aliases = [a.alias for a in _state(database).aliases.values() if a.store_key]
    assert store_aliases == [normalize_alias(f"KS ORG EGGS 24CT {MASKED_TEST_PAN}")]
    assert all("4111" not in alias for alias in store_aliases)


def test_s1_ac_21_masks_store_and_product_names(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """S1-AC-21: the store name and the import's product names are free text too.

    A product name becomes the product, its global alias and its FTS row; the store name
    becomes the store key of every alias and price.
    """
    draft = replace(receipt_draft(), store_name=f"COSTCO {TEST_PAN}")

    result = record(_named(draft, {0: f"{EGGS} {TEST_PAN}"}))

    assert result.masked_pans == 2
    assert result.receipt.store_name == f"COSTCO {MASKED_TEST_PAN}"
    (product,) = _state(database).products.values()
    assert product.canonical_name == f"{EGGS} {MASKED_TEST_PAN}"
    aliases = _state(database).aliases.values()
    assert {alias.alias for alias in aliases if alias.store_key is None} == {
        normalize_alias(f"{EGGS} {MASKED_TEST_PAN}")
    }
    assert all("4111" not in f"{alias.alias} {alias.store_key}" for alias in aliases)


def test_s1_ac_21_logs_the_outcome_but_never_the_text(record: RecordReceipt) -> None:
    """S1-AC-21 / PLAN 3.6: one receipt.recorded event with ids and counts, no receipt text."""
    with capture_logs() as logs:
        result = record(_with_pans(receipt_draft()))

    (event,) = [entry for entry in logs if entry["event"] == "receipt.recorded"]
    assert event["receipt_id"] == result.receipt.id
    assert event["status"] == ReceiptStatus.COMPLETED
    assert event["item_count"] == 2
    assert event["masked_pans"] == 3
    rendered = repr(logs)
    assert "4111" not in rendered
    assert "KS ORG EGGS" not in rendered
    assert "THANK YOU" not in rendered


# --- one unit of work -----------------------------------------------------------------------


def test_s1_ac_19_everything_is_written_in_one_unit_of_work(
    record: RecordReceipt, database: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-AC-19 / PLAN 3.6: a failure part-way through leaves nothing behind."""

    def explode(*_: object) -> tuple[()]:
        msg = "disk full"
        raise RuntimeError(msg)

    monkeypatch.setattr(FakePriceRepository, "add_many", explode)

    with pytest.raises(RuntimeError, match="disk full"):
        record(_named(receipt_draft(), {0: EGGS}))

    assert _state(database) == FakeState()
    assert database.commits == 0


def test_s1_ac_19_receipts_are_listed_after_recording(
    record: RecordReceipt, uow_factory: UnitOfWorkFactory
) -> None:
    """S1-AC-19: the recorded receipt is visible to a later unit of work."""
    saved = record(receipt_draft()).receipt

    with uow_factory() as uow:
        page = uow.receipts.list(PageRequest())

    assert [summary.id for summary in page.items] == [saved.id]


def test_s1_ac_19_a_blank_product_name_links_nothing(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """A product name that normalizes to nothing names no product: the line stays unlinked."""
    result = record(_named(receipt_draft(), {0: "  ", 1: MILK}))

    assert [item.product_id is None for item in result.receipt.items] == [True, False]
    assert [product.canonical_name for product in _state(database).products.values()] == [MILK]


def test_s1_ac_19_second_sight_adds_no_second_global_alias(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """The global alias is created with the product; seeing the product again only adds the
    store's spelling."""
    record(_named(receipt_draft(), {0: EGGS}))
    first = dict(_state(database).aliases)

    record(_named(receipt_draft(), {0: EGGS}))

    assert _state(database).aliases == first


def test_s1_ac_20_review_details_keep_missing_amounts_as_null(record: RecordReceipt) -> None:
    """S1-AC-20: an issue without an expected amount (a negative ITEM line) stores null."""
    eggs, milk = receipt_draft().items
    negative = replace(milk, unit_price=None, line_total=cad(-658))
    draft = receipt_draft(items=(eggs, negative), subtotal=cad(241), tax=None, total=cad(241))

    result = record(draft)

    assert result.review is not None
    issues = result.review.details["issues"]
    assert isinstance(issues, list)
    (issue,) = issues
    assert isinstance(issue, dict)
    assert issue["code"] == IssueCode.NEGATIVE_ITEM.value
    assert issue["expected"] is None
    assert issue["actual"] == {"amount_cents": -658, "currency": "CAD"}


# --- discounts and paid prices (docs/plans/discounts-and-paid-prices.md) --------------------

SIMPLE_NAMES = {0: EGGS, 1: MILK, 2: "Bananas", 3: "Kirkland Signature Paper Towels 12 rolls"}
SIMPLE_ATTRIBUTION = DiscountAttribution(
    links=(DiscountLink(4, 0, "1234567", new=True),), receipt_level=()
)


def _priced(database: FakeDatabase, saved: Receipt) -> dict[int, PriceObservation]:
    """The stored observations by the position of their line."""
    positions = {item.id: item.position for item in saved.items}
    return {positions[row.line_item_id]: row for row in _state(database).prices.values()}


def _paid(observation: PriceObservation) -> tuple[object, ...]:
    return (
        observation.unit_price,
        observation.line_total,
        observation.discount,
        observation.unit_discount,
        observation.paid,
        observation.on_sale,
    )


def test_tc_55a_assess_links_the_discount_and_writes_nothing(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """TC-55 (R7, A4): ``assess`` returns the draft with its new links and the attribution."""
    assessment = record.assess(simple_draft())

    assert assessment.draft == simple_draft(link=True)
    assert assessment.draft.items[4].applies_to_position == 0
    assert assessment.discounts == SIMPLE_ATTRIBUTION
    assert assessment.arithmetic.ok
    assert (database.commits, _state(database)) == (0, FakeState())


def test_tc_57_recording_stores_the_link_and_the_paid_prices(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """TC-57 (R5, R7): the import path links line 4 to the eggs, whose observation keeps its
    regular prices and adds discount -5.00, paid 3.99; the other lines are not on sale."""
    result = record(simple_draft(product_names=SIMPLE_NAMES))

    saved = result.receipt
    assert [item.applies_to_position for item in saved.items] == [None, None, None, None, 0]
    assert result.discounts == SIMPLE_ATTRIBUTION
    priced = _priced(database, saved)

    def expected(position: int, quantity: str, unit: int, total: int, discount: int) -> object:
        line = saved.items[position]
        return price_observation(
            id=priced[position].id,
            product_id=require_id(line.product_id),
            receipt_id=require_id(saved.id),
            line_item_id=require_id(line.id),
            observed_on=PURCHASED_ON,
            quantity=Decimal(quantity),
            unit_price=cad(unit),
            line_total=cad(total),
            discount=cad(discount),
        )

    assert priced == {
        0: expected(0, "1", 899, 899, -500),
        1: expected(1, "2", 329, 658, 0),
        2: expected(2, "1.36", 150, 204, 0),
        3: expected(3, "1", 1979, 1979, 0),
    }


def test_tc_118_an_inferred_unit_price_need_not_multiply_back_but_paid_is_exact(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """TC-118 (A8, Q19): 15 for 10.00 with no printed unit price: unit price 0.67 (66.67 half
    up; 0.67 x 15 = 10.05), unit discount -0.07, paid exactly 9.00. Nothing rejects it."""
    items = (
        item_line(0, CHOC, 1000, quantity=Decimal("15")),
        discount_line(1, "TPD/1362394", -100),
    )

    result = record(
        replace(draft_with(items, subtotal=900, tax=None, total=900), product_names={0: EGGS})
    )

    assert result.receipt.items[1].applies_to_position == 0
    (observation,) = _priced(database, result.receipt).values()
    assert observation.quantity == Decimal("15")
    assert _paid(observation) == (cad(67), cad(1000), cad(-100), cad(-7), cad(900), True)
    assert observation.paid_unit_price == cad(60)
