"""Alembic integration: the schema is versioned by hand-written migrations (ADR-0004).

The migration scripts live in the repository's top-level ``migrations/`` directory. The wheel
built for the runtime image ships them inside the package as ``paper_trail/migrations``.
"""

from functools import cache
from importlib import resources
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect, text

_SOURCE_CHECKOUT_SCRIPTS = Path(__file__).resolve().parents[4] / "migrations"
_VERSION_TABLE = "alembic_version"  # Alembic's default version table

DATABASE_PATH_ATTRIBUTE = "database_path"
CONNECTION_ATTRIBUTE = "connection"


def _script_location() -> Path:
    packaged = Path(str(resources.files("paper_trail") / "migrations"))
    return packaged if packaged.is_dir() else _SOURCE_CHECKOUT_SCRIPTS


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_script_location()))
    return config


def alembic_config(database_path: Path) -> Config:
    """An Alembic config whose ``env.py`` connects to ``database_path`` with ``create_engine``."""
    config = _config()
    config.attributes[DATABASE_PATH_ATTRIBUTE] = database_path
    return config


@cache
def head_revision() -> str:
    """The newest revision in the migration scripts."""
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:
        msg = "no Alembic revisions found"
        raise RuntimeError(msg)
    return head


def revision_of(connection: Connection) -> str | None:
    """The revision recorded in the database behind ``connection`` (None when unversioned).

    Reads the version table directly: Alembic's MigrationContext logs at INFO each time it
    is created, which would be noise from a readiness probe that runs every few seconds.
    """
    if not inspect(connection).has_table(_VERSION_TABLE):
        return None
    revision: str | None = connection.execute(
        text(f"SELECT version_num FROM {_VERSION_TABLE}")  # noqa: S608 - constant identifier
    ).scalar_one_or_none()
    return revision


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return revision_of(connection)


def upgrade_to_head(engine: Engine) -> None:
    """Upgrade the database to the head revision in one transaction. Idempotent."""
    with engine.begin() as connection:
        config = _config()
        config.attributes[CONNECTION_ATTRIBUTE] = connection
        command.upgrade(config, "head")
