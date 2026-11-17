"""FastAPI dependencies that hand route handlers what the composition root built."""

from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from paper_trail.bootstrap import Container, ReviewServices


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_reviews(request: Request) -> ReviewServices:
    reviews: ReviewServices | None = request.app.state.reviews
    if reviews is None:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "reviews are not served here")
    return reviews


ContainerDep = Annotated[Container, Depends(get_container)]
ReviewsDep = Annotated[ReviewServices, Depends(get_reviews)]
