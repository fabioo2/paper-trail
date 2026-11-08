# Contributing to Paper Trail

Everything runs in containers through `make`; you need only Docker, Make and Git. Run
`make hooks` once so every commit runs the same checks as CI, and `make check` before you push.

## How work is done (agent pipeline)

Each stage of [`docs/PLAN.md`](docs/PLAN.md) goes through four roles:

1. The **planner** writes or refines the stage plan with numbered acceptance criteria
   (`S<stage>-AC-<nn>`).
2. The **test author** turns every criterion into failing tests (see
   [`docs/tests/`](docs/tests/)) before any implementation exists.
3. The **implementer** makes the tests pass without weakening them, keeping `make check` green.
4. The **reviewer** checks the result against the same criteria; the implementer fixes findings.

Commits happen only when the project owner asks for them.

## Architecture and layering rules

The package is hexagonal (ADR-0002). `import-linter` enforces these rules in `make lint`, and
`tests/repo/test_import_contracts.py` checks that `domain` and `ports` import nothing outside the
standard library and their inner layers (any third-party package, listed anywhere or not):

| Layer | May import |
|---|---|
| `domain` | only `domain` and the standard library (no third-party packages) |
| `ports` | `domain` and the standard library |
| `application` | `domain`, `ports` (never `adapters`, `interfaces`, `agents`, `tools`, `bootstrap`) |
| `agents`, `tools` | `domain`, `ports`, `application` (never `adapters`, `interfaces`; `agents` never the storage ports: repositories, unit of work, queues, search) |
| `adapters` | `domain`, `ports`, `config` (never `interfaces`, `application`, `agents`) |
| `interfaces` | anything except `adapters`; wiring goes through `bootstrap` |
| `bootstrap` | the composition root: the only module that imports `adapters`, imported only by `interfaces` |

- `Settings` is created once, in `interfaces/cli/main.py`, and passed down. No other module reads
  the environment.
- Domain types are frozen dataclasses; enums are `StrEnum`; money is integer cents (ADR-0006).
- Storage is reached through a unit of work (`with uow_factory() as uow: ...; uow.commit()`);
  repositories never commit. A SQLite unit is one `BEGIN IMMEDIATE` transaction, so keep units
  short: they hold the database's write lock.
- HTTP errors are RFC 9457 problem details; every route sets a camelCase `operation_id`.
- After changing an API schema, run `make api-client` and commit `frontend/openapi.json` and
  `frontend/src/api/schema.d.ts`; `make api-check` fails on drift.

## Agents, tools and prompts

- Agent nodes (`src/paper_trail/agents/`) never touch storage: they call tools through the
  `ToolGateway` port as their role, and the registry (`tools/registry.py`) allows each tool to
  some roles only. Specialists only read; every write is a supervisor tool. `agents` and `tools`
  may not import each other; the JSON they exchange is defined once in
  `application/payloads.py`.
- A new tool is one `ToolSpec` in `build_tool_registry` (input and output models, roles) plus a
  sample payload in `tests/unit/tools/test_registry.py`, whose role x tool matrix is generated
  from the registry. The MCP server (`interfaces/mcp/server.py`) serves it with no further code;
  graph suites that use the `transport` fixture run once in-process and once over MCP.
- Untrusted text goes into a prompt only through `RenderedPrompt.messages`, which escapes the
  data-block tags in it (`neutralize_delimiters`).
- Routing is a pure function (`agents/routing.py`), tested exhaustively; tracing is added around
  nodes and ports by the runner, never inside a node.
- Prompts are versioned files: `agents/prompts/<agent>/<version>/` (`system.md`, `user.md`,
  `schema.json`, `examples/*.json`). Change a prompt by adding a version directory, so runs stay
  comparable (`--prompt-version`). `schema.json` must equal the Pydantic schema and every example
  must validate and add up; `tests/unit/agents/test_prompts.py` fails otherwise. Regenerate the
  schema with
  `make shell`, then
  `python -c "import json; from paper_trail.agents.schemas import ExtractionOutput as M; print(json.dumps(M.model_json_schema(), indent=2))" > src/paper_trail/agents/prompts/extraction/v1/schema.json`
  (`MatchDecision` for `matching/`).
- No test talks to a real model: graph tests use the scripted `tests/fakes/llm.py` and the
  deterministic `tests/fakes/embeddings.py`, adapter tests a respx-mocked host, CLI tests a fake
  Ollama on `127.0.0.1` (chat and `/api/embed`). An autouse fixture fails any
  test that connects beyond loopback.

## Test strategy

| Suite | Location | What it uses |
|---|---|---|
| Unit | `tests/unit/` | no I/O; in-memory fakes from `tests/fakes/` for every port |
| Contract | `tests/contract/` | one suite per port, run against the fake and the real adapter |
| Integration | `tests/integration/` | real adapters on a temporary SQLite file |
| End to end | `tests/e2e/` | the fully wired FastAPI app (`TestClient`), the CLI in-process (Typer's `CliRunner`) and one real CLI process per process boundary |
| Repository | `tests/repo/` | static checks of the Makefile, compose files, CI, docs |
| Frontend | `frontend/src/**/*.test.ts(x)`, `frontend/tests/tooling/` | Vitest, Testing Library, jsdom, MSW |
| Containers | `scripts/smoke.sh`, `tests/container/` | the running stack and the built image (`make ci`) |

- Markers (`integration`, `e2e`, `repo`) are applied from the directory. While you work, run
  `make test-fast`: unit, contract and integration suites in parallel (`pytest -n 6`, capped
  so the host's other services stay responsive), no coverage, well under a minute. `make test`
  is the full gate CI runs: every suite in parallel with branch coverage, failing under 90 %
  (`domain/` and `application/` are expected at 100 %). `make test-unit` (unit and contract)
  and `make test-integration` run one tier, serially.
- Tests run in parallel, so each one must be isolated: data under `tmp_path`, settings through
  `make_settings` or `monkeypatch.setenv` (an autouse fixture clears `PAPER_TRAIL_*`), server
  ports from the OS (`tests.support.free_port()` or port 0), never a fixed path or port.
- A test that needs a migrated database copies the session's template (`copy_migrated`,
  `migrated_settings`, `migrated_engine`) instead of running the migrations again;
  `tests/integration/test_migrations.py` is where migrations themselves are tested.
- CLI tests run the command in-process with `tests/e2e/agent_cli.invoke` (Typer's `CliRunner`).
  Spawn a real `python -m paper_trail` only for a new process boundary (signals, logging set up
  by the process, a long-running server): those live in `tests/e2e/test_cli_processes.py`.
- Graph-level tests run over the in-process tool gateway; whether the MCP transport changes
  anything is the job of `tests/unit/agents/test_transport_equivalence.py`, which runs
  representative scenarios over both and compares them. Add a scenario there when a change
  touches how tools are called.
- pytest runs with `--strict-markers --strict-config`, `xfail_strict` and warnings as errors.
- Pure rules (masking, arithmetic, money) get property tests with `hypothesis`.
- Agent nodes are tested with a scripted fake LLM so retry loops are deterministic; only the
  manual experiment runs (`make process`) talk to a real model.
- Test names carry the acceptance criterion they prove, e.g. `test_s0_ac_22_...`.
- Port contract suites (`tests/contract/`) take the `uow_factory` fixture, parametrized `fake` and
  `sqlite`: a new adapter behaviour is specified once and checked against both. Domain objects
  come from the builders in `tests/factories.py`.

## Adding a migration

Migrations are hand-written (no autogenerate) and numbered:

```sh
make revision m="receipt images" id=0003   # creates migrations/versions/0003_receipt_images.py
# edit upgrade() and downgrade()
make build migrate                      # rebuild the image, then apply it to the stack's database
```

The runtime image carries the migration scripts, so `make migrate` alone keeps applying the
migrations of the last build; always rebuild after adding or changing one (`make build up` also
works: the `migrate` service runs before `web` and `worker` start).

Every migration must downgrade cleanly; the integration suite runs `upgrade head`,
`downgrade base`, `upgrade head`. Migrations are frozen history: they import only Alembic,
SQLAlchemy and the standard library, never `paper_trail` (`tests/repo/test_migrations_frozen.py`),
so enum values are written into their CHECK constraints as literals. Adding a member to a stored
enum therefore needs a migration that widens the constraint; the schema tests fail until it
exists. Regular tables are mirrored in `adapters/sqlite/schema.py`, which must match the migrated
database (`compare_metadata`); FTS5 and `vec0` tables and their triggers live only in migrations. The database URL never goes into `alembic.ini`: the app builds
it from `Settings`, and `migrations/env.py` uses the application's own engine, so migrations see
the same pragmas, sqlite-vec and FTS5.

## Dependencies

- Python: edit `pyproject.toml` (lower bounds only), then `make lock` to update `uv.lock`.
- Frontend: `make fe-shell`, then `npm install <package>` to update `package.json` and
  `package-lock.json`. `node_modules` lives in a Docker volume; every frontend target first runs
  `make fe-deps`, which reinstalls it with `npm ci` whenever `package-lock.json` changed (after a
  Dependabot PR, for example). `make fe-install` forces a reinstall.
- Base images are pinned to a version line; Dependabot proposes weekly grouped upgrades, and
  `make outdated` shows the same report locally.
