"""Recorded receipts: the list (newest first, keyset-paged) and one receipt in full."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.paging import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, PageRequest
from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.receipts import ReceiptDetailOut, ReceiptPageOut

router = APIRouter(
    prefix="/receipts",
    tags=["receipts"],
    responses=problem_responses(HTTPStatus.UNPROCESSABLE_ENTITY, HTTPStatus.INTERNAL_SERVER_ERROR),
)


@router.get("", operation_id="listReceipts", summary="List receipts")
def list_receipts(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page")] = None,
    status: ReceiptStatus | None = None,
    store: Annotated[str | None, Query(description="Store name, in any spelling")] = None,
) -> ReceiptPageOut:
    """Receipts newest first. A bad cursor or limit is a 422 problem."""
    page = container.list_receipts(PageRequest(limit=limit, cursor=cursor), status, store)
    return ReceiptPageOut.from_domain(page)


@router.get(
    "/{receipt_id}",
    operation_id="getReceipt",
    summary="Get a receipt",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def get_receipt(container: ContainerDep, receipt_id: int) -> ReceiptDetailOut:
    """One receipt with its lines and an arithmetic report computed from the current data."""
    receipt, report = container.get_receipt(receipt_id)
    return ReceiptDetailOut.from_receipt(receipt, report)
