"""Keyset pagination newest first (created_at desc, id desc) with the domain's cursor, and the
receipts' variant that sorts by purchase date first."""

from collections.abc import Callable

from sqlalchemy import Connection, RowMapping, Select, Table, Text, and_, func, literal, or_, tuple_

from paper_trail.adapters.sqlite.types import UtcDateTime
from paper_trail.domain.paging import (
    Page,
    PageRequest,
    decode_cursor,
    decode_dated_cursor,
    encode_cursor,
    encode_dated_cursor,
)


def newest_first_page[T](
    connection: Connection,
    query: Select[tuple[object, ...]],
    table: Table,
    page: PageRequest,
    to_item: Callable[[RowMapping], T],
) -> Page[T]:
    """One page of ``query`` over ``table`` (which has ``created_at`` and ``id`` columns).

    The cursor names the last row shown; the next page starts strictly after it, so rows
    inserted meanwhile never shift a page. One extra row is read to know whether more remain.
    """
    created_at, row_id = table.c.created_at, table.c.id
    if page.cursor is not None:
        after_created_at, after_id = decode_cursor(page.cursor)
        query = query.where(
            or_(
                created_at < after_created_at,
                and_(created_at == after_created_at, row_id < after_id),
            )
        )
    query = query.order_by(created_at.desc(), row_id.desc()).limit(page.limit + 1)
    rows = connection.execute(query).mappings().all()
    shown = rows[: page.limit]
    next_cursor = (
        encode_cursor(shown[-1]["created_at"], shown[-1]["id"]) if len(rows) > page.limit else None
    )
    return Page(items=tuple(to_item(row) for row in shown), next_cursor=next_cursor)


def newest_purchase_first_page[T](
    connection: Connection,
    query: Select[tuple[object, ...]],
    table: Table,
    page: PageRequest,
    to_item: Callable[[RowMapping], T],
) -> Page[T]:
    """Like ``newest_first_page``, but by ``purchased_on`` desc first, undated rows last."""
    # ISO dates sort as text, and the empty text sorts below every date.
    day = func.coalesce(table.c.purchased_on, literal(""), type_=Text)
    key = tuple_(day, table.c.created_at, table.c.id)
    if page.cursor is not None:
        after_day, after_created_at, after_id = decode_dated_cursor(page.cursor)
        query = query.where(
            key
            < tuple_(
                literal("" if after_day is None else after_day.isoformat(), Text),
                literal(after_created_at, UtcDateTime),
                literal(after_id),
            )
        )
    query = query.order_by(day.desc(), table.c.created_at.desc(), table.c.id.desc()).limit(
        page.limit + 1
    )
    rows = connection.execute(query).mappings().all()
    shown = rows[: page.limit]
    next_cursor = (
        encode_dated_cursor(shown[-1]["purchased_on"], shown[-1]["created_at"], shown[-1]["id"])
        if len(rows) > page.limit
        else None
    )
    return Page(items=tuple(to_item(row) for row in shown), next_cursor=next_cursor)
