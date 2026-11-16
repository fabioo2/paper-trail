"""``LookupCache`` on SQLite: ``product_lookups``, one row per code and database (plan V10).

A row is a hit (state ``hit``, the hit as JSON) or a miss (``not_found``, no payload); an
error is never a row. Two processes remembering one answer at once leave one row: the second
write replaces the first. ``lookup_requests`` records every request to a rate-limited database
(UPCitemdb), whatever its answer, so its daily quota counts failures too.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.sqlite import insert

from paper_trail.adapters.sqlite.schema import lookup_requests, product_lookups
from paper_trail.ports.lookups import CachedLookup, LookupHit, LookupSource

HIT = "hit"
NOT_FOUND = "not_found"


class SqliteLookupCache:
    """Implements ``paper_trail.ports.lookups.LookupCache``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get(self, code: str, source: LookupSource) -> CachedLookup | None:
        row = (
            self._connection.execute(
                select(product_lookups).where(
                    product_lookups.c.code == code, product_lookups.c.source == source
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        payload = row["payload"]
        return CachedLookup(
            code=code,
            source=source,
            hit=None if payload is None else _hit(payload),
            fetched_at=row["fetched_at"],
        )

    def put(self, answer: CachedLookup) -> None:
        hit = answer.hit
        values = {
            "state": NOT_FOUND if hit is None else HIT,
            "payload": None if hit is None else _payload(hit),
            "fetched_at": answer.fetched_at,
        }
        self._connection.execute(
            insert(product_lookups)
            .values(code=answer.code, source=answer.source, **values)
            .on_conflict_do_update(index_elements=["code", "source"], set_=values)
        )

    def reserve_request(
        self, source: LookupSource, at: datetime, *, since: datetime, limit: int
    ) -> bool:
        used: int = self._connection.execute(
            select(func.count()).where(
                lookup_requests.c.source == source, lookup_requests.c.at >= since
            )
        ).scalar_one()
        if used >= limit:
            return False
        self._connection.execute(lookup_requests.insert().values(source=source, at=at))
        return True


def _payload(hit: LookupHit) -> dict[str, Any]:
    return {
        "name": hit.name,
        "brand": hit.brand,
        "quantity": hit.quantity,
        "categories": list(hit.categories),
    }


def _hit(payload: Any) -> LookupHit:  # noqa: ANN401 - a JSON document
    stored = cast("dict[str, Any]", payload)
    return LookupHit(
        name=stored["name"],
        brand=stored.get("brand"),
        quantity=stored.get("quantity"),
        categories=tuple(stored.get("categories") or ()),
    )
