"""Rows <-> domain objects, one pair of functions per entity.

``*_values`` build the column values to write (without ``id``); ``*_from_row`` build the domain
object from a ``RowMapping`` read through the tables of ``schema``, whose column types already
convert times, dates, decimals and enums. Money is split into cents columns plus one currency.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import RowMapping

from paper_trail.domain.models import (
    Correction,
    Job,
    LineItem,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    ReceiptSummary,
    ReviewItem,
)
from paper_trail.domain.money import Money

type Values = dict[str, Any]


def _cents(money: Money | None) -> int | None:
    return None if money is None else money.amount_cents


def _money(cents: int | None, currency: str) -> Money | None:
    return None if cents is None else Money(cents, currency)


def receipt_values(receipt: Receipt) -> Values:
    """Every column of ``receipts`` but ``id``; the items are written separately."""
    return {
        "status": receipt.status,
        "stage": receipt.stage,
        "store_name": receipt.store_name,
        "store_key": receipt.store_key,
        "purchased_on": receipt.purchased_on,
        "currency": receipt.currency,
        "subtotal_cents": _cents(receipt.subtotal),
        "tax_cents": _cents(receipt.tax),
        "total_cents": _cents(receipt.total),
        "payment_last4": receipt.payment_last4,
        "ocr_text": receipt.ocr_text,
        "image_path": receipt.image_path,
        "created_at": receipt.created_at,
        "updated_at": receipt.updated_at,
        "image_sha256": receipt.image_sha256,
    }


def receipt_from_row(row: RowMapping, items: Sequence[RowMapping]) -> Receipt:
    currency: str = row["currency"]
    return Receipt(
        id=row["id"],
        status=row["status"],
        stage=row["stage"],
        store_name=row["store_name"],
        store_key=row["store_key"],
        purchased_on=row["purchased_on"],
        currency=currency,
        subtotal=_money(row["subtotal_cents"], currency),
        tax=_money(row["tax_cents"], currency),
        total=_money(row["total_cents"], currency),
        payment_last4=row["payment_last4"],
        ocr_text=row["ocr_text"],
        image_path=row["image_path"],
        items=tuple(line_item_from_row(item, currency) for item in items),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        image_sha256=row["image_sha256"],
    )


def receipt_summary_from_row(row: RowMapping) -> ReceiptSummary:
    """A ``receipts`` row plus an ``item_count`` column."""
    return ReceiptSummary(
        id=row["id"],
        status=row["status"],
        store_name=row["store_name"],
        purchased_on=row["purchased_on"],
        total=_money(row["total_cents"], row["currency"]),
        item_count=row["item_count"],
        created_at=row["created_at"],
    )


def line_item_values(item: LineItem, receipt_id: int) -> Values:
    """The columns of ``line_items``; the amounts are in the receipt's currency."""
    return {
        "receipt_id": receipt_id,
        "position": item.position,
        "kind": item.kind,
        "raw_text": item.raw_text,
        "description": item.description,
        "quantity": item.quantity,
        "unit_price_cents": _cents(item.unit_price),
        "line_total_cents": item.line_total.amount_cents,
        "product_id": item.product_id,
        "category": item.category,
        "match_source": item.match_source,
    }


def line_item_from_row(row: RowMapping, currency: str) -> LineItem:
    return LineItem(
        id=row["id"],
        position=row["position"],
        kind=row["kind"],
        raw_text=row["raw_text"],
        description=row["description"],
        quantity=row["quantity"],
        unit_price=_money(row["unit_price_cents"], currency),
        line_total=Money(row["line_total_cents"], currency),
        product_id=row["product_id"],
        category=row["category"],
        match_source=row["match_source"],
    )


def product_values(product: Product) -> Values:
    return {
        "canonical_name": product.canonical_name,
        "brand": product.brand,
        "size_value": product.size_value,
        "size_unit": product.size_unit,
        "category": product.category,
        "tags": list(product.tags),
        "created_at": product.created_at,
    }


def product_from_row(row: RowMapping) -> Product:
    return Product(
        id=row["id"],
        canonical_name=row["canonical_name"],
        brand=row["brand"],
        size_value=row["size_value"],
        size_unit=row["size_unit"],
        category=row["category"],
        tags=tuple(row["tags"]),
        created_at=row["created_at"],
    )


def alias_values(alias: ProductAlias) -> Values:
    return {
        "product_id": alias.product_id,
        "alias": alias.alias,
        "store_key": alias.store_key,
        "source": alias.source,
        "created_at": alias.created_at,
    }


def alias_from_row(row: RowMapping) -> ProductAlias:
    return ProductAlias(
        id=row["id"],
        product_id=row["product_id"],
        alias=row["alias"],
        store_key=row["store_key"],
        source=row["source"],
        created_at=row["created_at"],
    )


def price_values(observation: PriceObservation) -> Values:
    return {
        "product_id": observation.product_id,
        "receipt_id": observation.receipt_id,
        "line_item_id": observation.line_item_id,
        "store_key": observation.store_key,
        "observed_on": observation.observed_on,
        "quantity": observation.quantity,
        "unit_price_cents": observation.unit_price.amount_cents,
        "line_total_cents": observation.line_total.amount_cents,
        "currency": observation.unit_price.currency,
    }


def price_from_row(row: RowMapping) -> PriceObservation:
    currency: str = row["currency"]
    return PriceObservation(
        id=row["id"],
        product_id=row["product_id"],
        receipt_id=row["receipt_id"],
        line_item_id=row["line_item_id"],
        store_key=row["store_key"],
        observed_on=row["observed_on"],
        quantity=row["quantity"],
        unit_price=Money(row["unit_price_cents"], currency),
        line_total=Money(row["line_total_cents"], currency),
    )


def correction_values(correction: Correction) -> Values:
    return {
        "kind": correction.kind,
        "subject": correction.subject,
        "old_value": correction.old_value,
        "new_value": correction.new_value,
        "store_key": correction.store_key,
        "receipt_id": correction.receipt_id,
        "line_item_id": correction.line_item_id,
        "created_at": correction.created_at,
    }


def correction_from_row(row: RowMapping) -> Correction:
    return Correction(
        id=row["id"],
        kind=row["kind"],
        subject=row["subject"],
        old_value=row["old_value"],
        new_value=row["new_value"],
        store_key=row["store_key"],
        receipt_id=row["receipt_id"],
        line_item_id=row["line_item_id"],
        created_at=row["created_at"],
    )


def job_from_row(row: RowMapping) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        receipt_id=row["receipt_id"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        locked_by=row["locked_by"],
        lease_expires_at=row["lease_expires_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def review_from_row(row: RowMapping) -> ReviewItem:
    return ReviewItem(
        id=row["id"],
        receipt_id=row["receipt_id"],
        reason=row["reason"],
        details=row["details"],
        status=row["status"],
        thread_id=row["thread_id"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )
