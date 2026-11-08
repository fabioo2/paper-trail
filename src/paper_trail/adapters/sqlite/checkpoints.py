"""LangGraph's SqliteSaver on the application's database file (ADR-0004, ADR-0008).

The saver keeps its tables (``checkpoints``, ``writes``) beside the application's and writes
through its own connection, configured like the engine's (WAL, busy timeout), so the graph's
checkpoints and the application's writes wait for each other instead of failing.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from paper_trail.adapters.sqlite.engine import CONNECTION_PRAGMAS


class CheckpointSaver(SqliteSaver):
    """SqliteSaver with plain, zero-padded channel versions.

    LangGraph's saver appends a random fraction to every channel version
    (``00…04.8296252511797759``). Sixteen random digits are a Luhn-valid card number one time
    in ten, so a scan for card numbers in the database (AC-A-04, ADR-0011) could no longer tell
    a leak from noise. The receipt graph has no parallel branches, so the counter alone orders
    versions; it stays compatible with versions written by the stock saver.
    """

    def get_next_version(self, current: str | None, channel: None) -> str:
        previous = 0 if current is None else int(str(current).split(".", maxsplit=1)[0])
        return f"{previous + 1:032}"


@contextmanager
def sqlite_checkpointer(database_path: Path) -> Iterator[CheckpointSaver]:
    with closing(sqlite3.connect(database_path, check_same_thread=False)) as connection:
        for pragma in CONNECTION_PRAGMAS:
            connection.execute(pragma)
        yield CheckpointSaver(connection)
