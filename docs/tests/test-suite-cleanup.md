# Test suite cleanup (after)

Owner-approved cleanup of the backend test suite, measured against `test-suite-baseline.md` on the same host (i7-6700, 8 logical cores), in the `dev` container, on 2026-11-06. The AC mapping docs (`stage0.md`, `stage1.md`, `agent-core-A.md`, `agent-core-B.md`, `agent-core-C.md`) were updated for every test moved, merged or removed below; every acceptance criterion still maps to at least one passing test.

## Results

| | Before | After |
|---|---:|---:|
| `make test` (coverage, wall clock incl. container start) | 472.3 s, serial | **85.4 to 89.3 s** (2 runs; 82.2 s of pytest inside `make ci`), `-n auto` (8 workers) |
| Unit + contract + integration, no coverage (`make test-fast`) | 90.8 s, serial (`pytest -m "not e2e and not repo"`) | **29.4 to 29.9 s** (2 runs), `-n auto` |
| Tests collected | 1,988 | 1,877 |
| Coverage (branch) | 97.75 % | 97.83 % |
| `python -c "import paper_trail.interfaces.cli.main"` | 2.42 s cumulative import | 1.03 s |

## What changed

1. **Lazy imports in the CLI (product change).** The composition root is now a package: `paper_trail.bootstrap` wires the application core (database, use cases, health) and `paper_trail.bootstrap.agents` wires the agent core (LangGraph, the MCP SDK, the Ollama and Tesseract adapters). `interfaces/cli/main.py` imports the web app, uvicorn and the agent command modules inside the commands that need them (ruff `PLC0415` is ignored for that one file), and the `--transport` option type moved to `interfaces/cli/options.py`. `db upgrade`, `db current`, `worker --check-heartbeat`, `version` and `--help` no longer import LangGraph, LangChain, LangSmith, the MCP SDK, httpx, FastAPI or uvicorn; `tests/e2e/test_cli_startup.py` guards it in a fresh interpreter. The import-linter contracts are unchanged and all kept.
2. **Migrated template database.** `tests/conftest.py` migrates one database per session (per xdist worker, `migrated_template`) and `copy_migrated(path)` copies it. `migrated_engine`, `migrated_settings`, the contract suites' SQLite `uow_factory` and the CLI suites' `migrated` fixtures use it. Migrations themselves are still tested from scratch in `tests/integration/test_migrations.py` and by the real `db upgrade` process test.
3. **CLI tests in-process.** `tests/e2e/test_import_and_query.py` runs `receipts import|show` through Typer's `CliRunner` ("no traceback" became "no exception escaped the command"). `tests/e2e/test_cli_processes.py` keeps one real `python -m paper_trail` process per boundary: `db upgrade`, `serve`, `worker` (SIGTERM) and, new, `process` end to end against the fake Ollama; `test_mcp_server.py` keeps the standalone `mcp` process. `db current`, the second `db upgrade`, `worker --check-heartbeat` and `mcp` on an unmigrated database run in-process.
4. **Parallel runs.** `pytest-xdist` (dev group, locked) runs `make test` and `make test-fast` with `-n auto`; pytest-cov combines the workers' and their child processes' coverage. The server tests take their ports from the OS through `tests.support.free_port()`; every test already used `tmp_path` and a cleared `PAPER_TRAIL_*` environment.
5. **Transport equivalence instead of duplication.** The graph and review suites (`unit/agents/test_receipt_graph.py`, `test_receipt_graph_tools.py`, `test_matching_graph.py`, `test_resume.py`, `unit/application/test_resolve_review.py`, `integration/test_receipt_graph_sqlite.py`, `test_matching_sqlite.py`) run in-process only. `tests/unit/agents/test_transport_equivalence.py` runs six scenarios (clean run with matching, validation retry, review routing, denied tool, resume in a second process, review resolution) over both transports and requires equal outcomes, trace events (minus timing), stored receipt, reviews, model calls and `tool.denied` logs; each scenario also asserts its own expected result. The port contract suites `contract/test_tool_gateway.py` and `contract/test_matching_tools.py` still run over both gateways: that is what a contract suite is for.
6. **Documentation-wording tests removed** (the REMOVE rows of the baseline, section (e)); the KEEP rows stay. S0-AC-36 now maps to `test_s0_ac_36_relative_links_resolve` (every repository path README and CONTRIBUTING link to exists); `test_s0_ac_39_windows_llm_host_steps_need_no_make` lost only its `ezwinports` wording assertion.
7. **`make test-fast`**: unit, contract and integration in parallel, no coverage; documented in CONTRIBUTING and the README target table.

## Test count, 1,988 to 1,877 (-111)

| Change | Items |
|---|---:|
| `[mcp]` twins of the graph and review suites dropped; the `[inprocess]` half kept, unparametrized (79 tests) | -79 |
| `test_transport_equivalence.py`: 6 scenarios + 1 guard that the MCP runs really use `McpToolGateway` | +7 |
| Documentation-wording tests removed: `test_repo_hygiene.py` S0-AC-36 README/CONTRIBUTING/ADR checks (34), `test_agent_core_repo.py::test_readme_documents_make_process`, `test_agent_core_b_repo.py::test_readme_documents_the_tools_and_the_mcp_server` and `::test_review_note_5_*` (2), `test_agent_core_c_repo.py::test_review_note_2_*`, `::test_readme_documents_the_slice_c_commands`, `::test_the_e2_commands_are_runnable_as_written` | -41 |
| `test_s0_ac_36_relative_links_resolve[README.md, CONTRIBUTING.md]` | +2 |
| `test_cli_processes.py`: `db upgrade` at head + alembic JSON merged into one process test; the two worker SIGTERM tests merged into one (idle poll longer than the grace, heartbeat fresh, exit within the grace, stale afterwards) | -2 |
| `test_cli_processes.py::test_ac_a_10_process_runs_a_receipt_through_the_agents_in_a_real_process`, `test_cli_startup.py` | +2 |

Moved without a count change: the 14 `test_import_and_query.py` tests, `test_s0_ac_23_db_upgrade_twice_is_a_no_op`, `test_s0_ac_24_check_heartbeat_fails_when_file_is_missing` and `test_s5_ac_03_mcp_refuses_an_unmigrated_database` now run in-process.

## Kept on purpose

- Repository tests that guard real properties: loopback-only ports, pinned and Dependabot-parseable base images, no secrets, containerized Make recipes, compose hardening, import contracts, CI calling Make targets, `.env.example` drift and secrets, the Windows LLM commands matching `make llm-up/down`, and `test_the_compose_run_macro_is_not_a_user_variable` (its README `RUN=` line guards a command users copy).
- The two port contract suites over both tool transports (above).
- `tests/integration/test_database_health_probe.py` migrates its own engine inside the test: the migration is part of what those tests check.
- Review notes 2 and 5 (plan status and PLAN 7 wording) are documentation state, now checked in review, not by a test.
