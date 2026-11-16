"""The product-database adapters over a canned HTTP transport (claude-extraction plan D3, R1,
V10; CE-TC-207..210, 259). No socket is opened: ``httpx2.MockTransport`` answers every request.

Pinned:
- Open*Facts: GET ``https://<host>/api/v2/product/<code>.json`` (fields asked in the query),
  UPCitemdb: GET ``https://api.upcitemdb.com/prod/trial/lookup?upc=<code>``. No key, no auth,
  no cookie: the User-Agent is the only header the app adds, and the barcode is the only data.
- A hit needs a name; a 404, an unknown code or a nameless product is a miss (None); a 429 is
  ``LookupRateLimitedError``; any other error status, a timeout, a lost connection or a body
  that is not JSON is ``LookupFailedError``.
"""

from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from paper_trail.adapters.lookups.http import (
    LOOKUP_HOSTS,
    OPEN_BEAUTY_FACTS,
    OPEN_FOOD_FACTS,
    OPEN_PRODUCTS_FACTS,
    UPCITEMDB,
    USER_AGENT,
    lookup_client,
)
from paper_trail.adapters.lookups.openfacts import FIELDS, OpenFactsLookup
from paper_trail.adapters.lookups.upcitemdb import UpcItemDbLookup
from paper_trail.ports.lookups import (
    LookupFailedError,
    LookupHit,
    LookupRateLimitedError,
    ProductLookup,
)

GTIN = "4006381333931"
OFF_PRODUCT = {
    "status": 1,
    "product": {
        "product_name": " Organic Eggs ",
        "brands": "Acme, Acme Group",
        "quantity": "500 g",
        "categories": "Dairies, Eggs",
    },
}
UPC_ITEMS = {
    "code": "OK",
    "total": 1,
    "items": [{"title": "Organic Eggs", "brand": "Acme", "size": "1 kg", "category": "Food"}],
}

type Handler = Callable[[httpx2.Request], httpx2.Response]


def _adapters(handler: Handler) -> dict[str, ProductLookup]:
    client = lookup_client(10.0, transport=httpx2.MockTransport(handler))
    return {
        "off": OpenFactsLookup("off", OPEN_FOOD_FACTS, client),
        "obf": OpenFactsLookup("obf", OPEN_BEAUTY_FACTS, client),
        "opf": OpenFactsLookup("opf", OPEN_PRODUCTS_FACTS, client),
        "upcitemdb": UpcItemDbLookup(client),
    }


def _answer(status: int, body: Any = None, *, text: str | None = None) -> Handler:
    def handler(_: httpx2.Request) -> httpx2.Response:
        if text is not None:
            return httpx2.Response(status, text=text)
        return httpx2.Response(status, json=body)

    return handler


def _raise(error: Exception) -> Handler:
    def handler(_: httpx2.Request) -> httpx2.Response:
        raise error

    return handler


def test_the_requests_carry_the_barcode_and_the_user_agent_only() -> None:
    requests: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(404)

    for adapter in _adapters(record).values():
        adapter.lookup(GTIN)

    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", f"https://{OPEN_FOOD_FACTS}/api/v2/product/{GTIN}.json?fields={_query(FIELDS)}"),
        ("GET", f"https://{OPEN_BEAUTY_FACTS}/api/v2/product/{GTIN}.json?fields={_query(FIELDS)}"),
        (
            "GET",
            f"https://{OPEN_PRODUCTS_FACTS}/api/v2/product/{GTIN}.json?fields={_query(FIELDS)}",
        ),
        ("GET", f"https://{UPCITEMDB}/prod/trial/lookup?upc={GTIN}"),
    ]
    for request in requests:
        assert request.headers["user-agent"] == USER_AGENT
        assert request.content == b""
        assert {"authorization", "cookie", "x-api-key"}.isdisjoint(request.headers)
    assert {request.url.host for request in requests} == LOOKUP_HOSTS
    assert "@" not in USER_AGENT
    assert not any("costco" in host for host in LOOKUP_HOSTS)


def _query(fields: str) -> str:
    return fields.replace(",", "%2C")


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (
            _answer(200, OFF_PRODUCT),
            LookupHit("Organic Eggs", "Acme", "500 g", ("Dairies", "Eggs")),
        ),
        (_answer(200, {"status": 1, "product": {"product_name": "Eggs"}}), LookupHit("Eggs")),
        (_answer(200, {"status": 1, "product": {"brands": "Acme"}}), None),
        (_answer(200, {"status": 0, "status_verbose": "product not found"}), None),
        (_answer(404, {"status": 0}), None),
    ],
    ids=["hit", "name-only", "no-name", "status-0", "404"],
)
def test_open_facts_answers(handler: Handler, expected: LookupHit | None) -> None:
    assert _adapters(handler)["off"].lookup(GTIN) == expected


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (_answer(200, UPC_ITEMS), LookupHit("Organic Eggs", "Acme", "1 kg", ("Food",))),
        (_answer(200, {"code": "OK", "total": 0, "items": []}), None),
        (_answer(400, {"code": "INVALID_UPC"}), None),
        (_answer(404, {"code": "NOT_FOUND"}), None),
    ],
    ids=["hit", "no-items", "400", "404"],
)
def test_upcitemdb_answers(handler: Handler, expected: LookupHit | None) -> None:
    assert _adapters(handler)["upcitemdb"].lookup(GTIN) == expected


@pytest.mark.parametrize("source", ["off", "upcitemdb"])
@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (_answer(429, {}), LookupRateLimitedError),
        (_answer(500, {}), LookupFailedError),
        (_answer(200, text="<html>busy</html>"), LookupFailedError),
        (_answer(200, {}), LookupFailedError),
        (_answer(200, {"error": "server busy", "code": "ERROR"}), LookupFailedError),
        (_raise(httpx2.ReadTimeout("slow")), LookupFailedError),
        (_raise(httpx2.ConnectError("refused")), LookupFailedError),
    ],
    ids=["429", "500", "not-json", "empty-object", "error-object", "timeout", "no-connection"],
)
def test_failures_are_errors_never_misses(
    source: str, handler: Handler, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _adapters(handler)[source].lookup(GTIN)


def test_a_redirect_is_not_followed() -> None:
    """A redirect could leave the lookup hosts: it is an error status, not a new request."""
    requests: list[str] = []

    def redirect(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url.host))
        return httpx2.Response(302, headers={"location": "https://example.com/"})

    with pytest.raises(LookupFailedError):
        _adapters(redirect)["off"].lookup(GTIN)
    assert requests == [OPEN_FOOD_FACTS]
