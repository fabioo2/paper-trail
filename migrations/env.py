"""Alembic environment.

The database comes from the Alembic config the caller built (see
``paper_trail.adapters.sqlite.migrations``): either an open connection, or a database path that
is opened with the application's own ``create_engine`` so migrations see the same pragmas,
sqlite-vec and FTS5. Logging is configured by the CLI, never here.
"""

from alembic import context
from sqlalchemy import Connection

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import CONNECTION_ATTRIBUTE, DATABASE_PATH_ATTRIBUTE


def _run(connection: Connection) -> None:
    context.configure(connection=connection, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    attributes = context.config.attributes
    connection: Connection | None = attributes.get(CONNECTION_ATTRIBUTE)
    if connection is not None:
        _run(connection)
        return
    database_path = attributes.get(DATABASE_PATH_ATTRIBUTE)
    if database_path is None:
        msg = "no database configured: run migrations with `paper-trail db upgrade`"
        raise RuntimeError(msg)
    engine = create_engine(database_path)
    try:
        with engine.begin() as new_connection:
            _run(new_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    msg = "offline (--sql) migrations are not supported"
    raise RuntimeError(msg)
run_migrations()
