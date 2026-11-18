"""The month summary shown on Home."""

from datetime import date
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.summary import MonthSummaryOut

router = APIRouter(
    tags=["summary"],
    responses=problem_responses(HTTPStatus.UNPROCESSABLE_ENTITY, HTTPStatus.INTERNAL_SERVER_ERROR),
)


@router.get("/summary", operation_id="getSummary", summary="Get a month's summary")
def get_summary(
    container: ContainerDep,
    month: Annotated[
        str | None,
        Query(
            pattern=r"^[1-9]\d{3}-(0[1-9]|1[0-2])$",
            description="YYYY-MM; the server's current month by default",
        ),
    ] = None,
) -> MonthSummaryOut:
    """Spending of the month's completed receipts by purchase date, by week and by store, plus
    what is waiting now (open reviews, unconfirmed products, receipts being read) and the newest
    receipts. A malformed month is a 422 problem."""
    first = None if month is None else date.fromisoformat(f"{month}-01")
    return MonthSummaryOut.from_domain(container.get_month_summary(first))
