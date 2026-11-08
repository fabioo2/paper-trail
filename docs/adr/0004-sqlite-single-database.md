# 4. One SQLite database with FTS5 and sqlite-vec

Date: 2026-11-06

## Status

Accepted

## Context

Single user, a few thousand receipts at most, one home server, data must stay private. We need relational data, full-text search, vector similarity search, a job queue and LangGraph checkpoints.

## Decision

- One SQLite file under the data volume, WAL mode, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`, set on every connection by an SQLAlchemy `connect` event that also loads the `sqlite-vec` extension.
- SQLAlchemy 2.0 Core (no ORM) with explicit mappers to domain dataclasses.
- Alembic migrations are hand-written (`render_as_batch=True`). FTS5 external-content tables with sync triggers and `vec0` virtual tables are created with `op.execute`. A test asserts the migrated schema matches the SQLAlchemy metadata for regular tables.
- Integer primary keys (`INTEGER PRIMARY KEY`) so FTS5 and `vec0` rowids map directly to entity ids.
- LangGraph's `SqliteSaver` tables live in the same file but are created by the saver's `setup()` and excluded from Alembic via `include_object`.
- Embedding dimension (768, nomic-embed-text) is fixed in the migration; changing the model means a new migration and re-embedding. The readiness check reports the dimension of `product_vectors` and fails when it is not 768.
- Transactions are begun by SQLAlchemy, not by the `sqlite3` driver (its implicit transaction handling is switched off on connect; a `begin` event issues `BEGIN`). A writing unit of work begins with `BEGIN IMMEDIATE`, taking the write lock up front: a deferred transaction that reads and then writes fails at once with "database is locked" if another connection wrote in between, whereas an immediate one waits for `busy_timeout`. A read-only unit of work (`uow_factory(read_only=True)`, used by queries such as `GetReceipt` and `ListReceipts`) begins `DEFERRED` with `PRAGMA query_only=ON`, so in WAL mode it reads its snapshot while a writer holds the lock and any write through it fails. Every `begin` sets `query_only` explicitly, so a pooled connection never inherits it. Other transactions (probes, migrations) stay deferred. (Added in Stage 1.)

## Consequences

- Zero extra services; backup is one online `sqlite3` backup plus the images directory.
- Single writer: web and worker contend for the write lock. Acceptable at this volume; `busy_timeout` and short transactions keep it invisible.
- Official Python images compile `sqlite3` with loadable-extension support; the readiness check verifies `vec_version()` and FTS5 on startup.

## Amendment (2026-11-06): agent core slice A

LangGraph's checkpoints (`checkpoints`, `writes` tables, created by the saver, never by a migration) live in the same file. The saver writes through its own connection with the engine's pragmas (WAL, `busy_timeout`), so graph checkpoints and application writes wait for each other. A `paper-trail process --run NAME` gets its own database file under `DATA_DIR/runs/NAME/`, migrated on first use, so experiment runs never mix with the main database.
