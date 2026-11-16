"""UPCitemdb's free trial endpoint (``/prod/trial/lookup?upc=<code>``, no key, 100 requests a
day): asked last, only when ``LOOKUP__UPCITEMDB`` is on.

A code it knows comes back as the first of ``items`` (with ``code`` ``OK``); ``OK`` with no
items, or a 400/404 (a code it does not take), is a miss. A 429 is a rate limit; any other error
status, a timeout, a lost connection, a body that is not JSON or any other body is a failure,
so it is never remembered as a miss.
"""

from typing import Any

import httpx2

from paper_trail.adapters.lookups.http import NOT_FOUND, TOO_MANY_REQUESTS, UPCITEMDB
from paper_trail.ports.lookups import (
    LookupFailedError,
    LookupHit,
    LookupRateLimitedError,
    LookupSource,
)

URL = f"https://{UPCITEMDB}/prod/trial/lookup"
_BAD_REQUEST = 400


class UpcItemDbLookup:
    """Implements ``paper_trail.ports.lookups.ProductLookup``."""

    def __init__(self, client: httpx2.Client) -> None:
        self._client = client

    @property
    def source(self) -> LookupSource:
        return "upcitemdb"

    def lookup(self, code: str) -> LookupHit | None:
        try:
            response = self._client.get(URL, params={"upc": code})
        except httpx2.HTTPError as error:
            msg = f"{UPCITEMDB} cannot be reached: {type(error).__name__}"
            raise LookupFailedError(msg) from None
        if response.status_code == TOO_MANY_REQUESTS:
            msg = f"{UPCITEMDB} answered 429"
            raise LookupRateLimitedError(msg)
        if response.status_code in {_BAD_REQUEST, NOT_FOUND}:
            return None
        if not response.is_success:
            msg = f"{UPCITEMDB} answered {response.status_code}"
            raise LookupFailedError(msg)
        try:
            body = response.json()
        except ValueError:
            msg = f"{UPCITEMDB} answered with a body that is not JSON"
            raise LookupFailedError(msg) from None
        return _hit(body)


def _hit(body: Any) -> LookupHit | None:  # noqa: ANN401 - a JSON document
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("code") != "OK" or not isinstance(items, list):
        msg = f"{UPCITEMDB} answered a body that is neither items nor a miss"
        raise LookupFailedError(msg)
    if not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        msg = f"{UPCITEMDB} answered an item that is not an object"
        raise LookupFailedError(msg)
    name = _text(first.get("title"))
    if name is None:
        return None
    category = _text(first.get("category"))
    return LookupHit(
        name=name,
        brand=_text(first.get("brand")),
        quantity=_text(first.get("size")),
        categories=() if category is None else (category,),
    )


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None
