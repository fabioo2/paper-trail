"""Reviews: the receipts waiting for a person, one with the reading it holds, and a person's
answer, which the worker saves."""

from http import HTTPStatus

from fastapi import APIRouter

from paper_trail.interfaces.web.dependencies import ReviewsDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.reviews import (
    ResolveReviewIn,
    ReviewAnswerOut,
    ReviewOut,
    ReviewSummaryOut,
)

router = APIRouter(
    prefix="/reviews",
    tags=["reviews"],
    responses=problem_responses(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.SERVICE_UNAVAILABLE,
    ),
)


@router.get("", operation_id="listReviews", summary="List open reviews")
def list_reviews(reviews: ReviewsDep) -> list[ReviewSummaryOut]:
    """The open reviews a person can answer now, newest first, whatever their receipt's
    status; one whose answer is queued or running is left out."""
    return [ReviewSummaryOut.from_pending(pending) for pending in reviews.list_reviews()]


@router.get(
    "/{review_id}",
    operation_id="getReview",
    summary="Get a review",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def get_review(reviews: ReviewsDep, review_id: int) -> ReviewOut:
    """One review with its issues, the reading it holds and its photo; an unknown id is a 404
    problem."""
    return ReviewOut.from_pending(reviews.get_review(review_id))


@router.post(
    "/{review_id}/resolve",
    operation_id="resolveReview",
    summary="Answer a review",
    status_code=HTTPStatus.ACCEPTED,
    responses=problem_responses(HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT),
)
def resolve_review(reviews: ReviewsDep, review_id: int, body: ResolveReviewIn) -> ReviewAnswerOut:
    """Accept the reading as read or save a corrected one; the worker resumes the receipt with
    it. A reading that does not add up is a 422 problem of type /problems/inconsistent-receipt
    listing each issue in ``errors``, unless ``force`` is set with a ``reason``. An unknown
    review is a 404 problem; one already resolved, or already answered, a 409 problem."""
    draft = None if body.draft is None else body.draft.to_draft()
    receipt = reviews.answer_review(review_id, draft, force=body.force, reason=body.reason)
    return ReviewAnswerOut.from_receipt(receipt)
