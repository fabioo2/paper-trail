"""The HTTP client every lookup shares, and the only hosts a lookup may contact.

The User-Agent names the app and its repository, never a person, an e-mail address or an IP
(Open Food Facts asks every client to identify itself). No store's site is ever one of the
hosts.
"""

import httpx2

USER_AGENT = "PaperTrail/1.0 (personal receipt app; +https://github.com/fabioo2/paper-trail)"
OPEN_FOOD_FACTS = "world.openfoodfacts.org"
OPEN_BEAUTY_FACTS = "world.openbeautyfacts.org"
OPEN_PRODUCTS_FACTS = "world.openproductsfacts.org"
UPCITEMDB = "api.upcitemdb.com"
LOOKUP_HOSTS = frozenset({OPEN_FOOD_FACTS, OPEN_BEAUTY_FACTS, OPEN_PRODUCTS_FACTS, UPCITEMDB})
TOO_MANY_REQUESTS = 429
NOT_FOUND = 404


def lookup_client(timeout_s: float, transport: httpx2.BaseTransport | None = None) -> httpx2.Client:
    """A client that sends only the User-Agent and follows no redirect (a redirect could leave
    ``LOOKUP_HOSTS``); ``transport`` is a test's canned one."""
    return httpx2.Client(
        timeout=timeout_s,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
        transport=transport,
    )
