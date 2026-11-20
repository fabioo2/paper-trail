"""Open Food Facts, Open Beauty Facts and Open Products Facts: one v2 product endpoint, no key
(``https://<host>/api/v2/product/<code>.json``). Their data is under the ODbL
(docs/how-it-works.md).

A product the database knows has ``status`` 1 and a ``product``; one without a name gives
nothing to name with, so it is a miss like ``status`` 0 or a 404. A 429 is a rate limit; any
other error status, a timeout, a lost connection, a body that is not JSON or any other body (an
error object, ``{}``) is a failure, so it is never remembered as a miss.
"""

from typing import Any

import httpx2

from paper_trail.adapters.lookups.http import NOT_FOUND, TOO_MANY_REQUESTS
from paper_trail.ports.lookups import (
    LookupFailedError,
    LookupHit,
    LookupRateLimitedError,
    LookupSource,
)

FIELDS = "product_name,brands,quantity,categories"


class OpenFactsLookup:
    """Implements ``paper_trail.ports.lookups.ProductLookup`` for one Open*Facts database."""

    def __init__(self, source: LookupSource, host: str, client: httpx2.Client) -> None:
        self._source: LookupSource = source
        self._host = host
        self._client = client

    @property
    def source(self) -> LookupSource:
        return self._source

    def lookup(self, code: str) -> LookupHit | None:
        url = f"https://{self._host}/api/v2/product/{code}.json"
        try:
            response = self._client.get(url, params={"fields": FIELDS})
        except httpx2.HTTPError as error:
            msg = f"{self._host} cannot be reached: {type(error).__name__}"
            raise LookupFailedError(msg) from None
        if response.status_code == TOO_MANY_REQUESTS:
            msg = f"{self._host} answered 429"
            raise LookupRateLimitedError(msg)
        if response.status_code == NOT_FOUND:
            return None
        if not response.is_success:
            msg = f"{self._host} answered {response.status_code}"
            raise LookupFailedError(msg)
        try:
            body = response.json()
        except ValueError:
            msg = f"{self._host} answered with a body that is not JSON"
            raise LookupFailedError(msg) from None
        return _hit(self._host, body)


def _hit(host: str, body: Any) -> LookupHit | None:  # noqa: ANN401 - a JSON document
    status = body.get("status") if isinstance(body, dict) else None
    product = body.get("product") if isinstance(body, dict) else None
    if status == 0:
        return None
    if status != 1 or not isinstance(product, dict):
        msg = f"{host} answered a body that is neither a product nor a miss"
        raise LookupFailedError(msg)
    name = _text(product.get("product_name"))
    if name is None:
        return None
    brands = _text(product.get("brands"))
    categories = _text(product.get("categories"))
    return LookupHit(
        name=name,
        brand=None if brands is None else _text(brands.split(",")[0]),
        quantity=_text(product.get("quantity")),
        categories=()
        if categories is None
        else tuple(part.strip() for part in categories.split(",") if part.strip()),
    )


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None
