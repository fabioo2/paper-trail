"""Recorded receipts: the list (newest first, keyset-paged), one receipt in full, and the
upload of a photo for the worker to process."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, UploadFile

from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.paging import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, PageRequest
from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses, too_large_detail
from paper_trail.interfaces.web.schemas.receipts import (
    ReceiptDetailOut,
    ReceiptPageOut,
    ReceiptUploadOut,
)

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


@router.post(
    "",
    operation_id="uploadReceipt",
    summary="Upload a receipt photo",
    status_code=HTTPStatus.ACCEPTED,
    responses={
        HTTPStatus.OK.value: {
            "model": ReceiptUploadOut,
            "description": "The photo was uploaded before: its receipt, not processed again.",
        },
        **problem_responses(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
    },
)
def upload_receipt(
    container: ContainerDep, file: UploadFile, response: Response
) -> ReceiptUploadOut:
    """Store a JPEG, PNG, WebP or HEIC photo and queue its receipt for the worker; poll
    GET /api/receipts/{id} for its status. A file above UPLOAD__MAX_BYTES is a 413 problem;
    anything but a photo, or a photo above the pixel limit, is a 415 problem."""
    max_bytes = container.settings.upload.max_bytes
    image = file.file.read(max_bytes + 1)
    if len(image) > max_bytes:
        raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, too_large_detail(max_bytes))
    upload = container.upload_receipt(image)
    if upload.duplicate:
        response.status_code = HTTPStatus.OK
    return ReceiptUploadOut.from_domain(upload.receipt)


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
