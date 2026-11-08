"""``TextSearchIndex`` over the FTS5 tables ``line_items_fts`` and ``products_fts``.

User text never reaches FTS5 as query syntax: it is split into word tokens (maximal runs of
Unicode letters and digits) and each token becomes a quoted prefix term, ``"tok"*``. Quotes,
operators (AND, OR, NOT, NEAR), column filters and parentheses are therefore plain text, and
all terms must match. Case and diacritics are ignored by the tokenizer on both sides.
"""

import re
import unicodedata

from sqlalchemy import Connection, text

from paper_trail.ports.search import HitKind, TextHit

_TOKEN = re.compile(r"[^\W_]+")

# bm25() is lower-is-better and negative; the port's score is its negation (higher is better).
# The kind and id order hits of equal score deterministically.
_SEARCH = text(
    f"""
    SELECT '{HitKind.LINE_ITEM}' AS kind, rowid AS id, -bm25(line_items_fts) AS score
    FROM line_items_fts WHERE line_items_fts MATCH :query
    UNION ALL
    SELECT '{HitKind.PRODUCT}' AS kind, rowid AS id, -bm25(products_fts) AS score
    FROM products_fts WHERE products_fts MATCH :query
    ORDER BY score DESC, kind, id
    LIMIT :limit
    """  # noqa: S608 - the interpolated values are enum constants, the query is a parameter
)


def fts_query(user_text: str) -> str | None:
    """The FTS5 query for ``user_text``: every word as a quoted prefix term; None if no words."""
    tokens = _TOKEN.findall(unicodedata.normalize("NFKC", user_text))
    return " ".join(f'"{token}"*' for token in tokens) or None


class SqliteTextSearchIndex:
    """Implements ``paper_trail.ports.search.TextSearchIndex``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def search(self, query: str, *, limit: int) -> tuple[TextHit, ...]:
        match = fts_query(query)
        if match is None:
            return ()
        rows = self._connection.execute(_SEARCH, {"query": match, "limit": limit})
        return tuple(
            TextHit(kind=HitKind(kind), id=hit_id, score=float(score))
            for kind, hit_id, score in rows
        )
