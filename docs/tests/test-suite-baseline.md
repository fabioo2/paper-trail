# Test suite baseline (before the cleanup)

Measured on 2026-11-06 by the baseline agent, before any change to code or tests. It is the reference that the test-suite cleanup is compared against. Nothing in `src/` or `tests/` was modified to take these numbers.

**Setup.** Host: Intel i7-6700 (8 logical cores), 16 GB RAM, SSD. Everything ran in the `dev` container (`docker compose run --rm dev`, Python 3.12.16, pytest 9.2.0, Docker Compose 5.6.0), serially (no `pytest-xdist`), with the web and worker containers of the runtime stack up but idle. All 1,988 tests were collected and passed in every run (the earlier audit ran with 19 files uncollectable, so its 172 s is not comparable).

## (a) Wall time

| Run | Command | pytest reports | Wall clock (host, incl. container start) |
|---|---|---|---|
| `make test` (with coverage) | `pytest --cov --cov-report=term --cov-report=xml --cov-fail-under=90` | 465.06 s (7 min 45 s) | **472.3 s (7 min 52 s)** |
| Plain pytest | `pytest --durations=0 --junitxml=...` | 266.73 s (4 min 27 s) | **272.2 s (4 min 32 s)** |
| Plain pytest, repeat (with a spawn-recording plugin) | `pytest -q -p spawnprobe` | 262.64 s | |

- Coverage costs about **+200 s (+74 %)**. `[tool.coverage.run] patch = ["subprocess"]` also measures every spawned `python -m paper_trail` child, so the subprocess-heavy e2e tests pay the overhead twice (parent and child).
- Container start-up adds about 5 to 6 s per `make` target.
- The sum of per-test times in the JUnit report is 260.9 s; the remaining ~6 s is collection and session start.

### Root-cause spot checks

| Measurement (inside the dev container) | Time |
|---|---|
| `python -c "pass"` | 0.02 s |
| `python -c "import paper_trail.interfaces.cli.main"` (3 runs) | 2.68 to 2.72 s |
| `python -m paper_trail db upgrade` on an empty data dir (2 runs) | 2.72 to 2.73 s |
| `-X importtime`: `paper_trail.bootstrap` cumulative | 1.59 s |
| `-X importtime`: `paper_trail.adapters.mcp.gateway` (pulls in `mcp`) | 0.55 s |
| `-X importtime`: `paper_trail.agents.graph` (pulls in `langgraph`, `langchain_core`) | 0.45 s |

So `db upgrade` is almost entirely CLI import time: the migration itself is a few tens of milliseconds.

## (b) Durations

### Top 40 phases (`pytest --durations`, plain run)

| Time | Phase | Test |
|---|---|---|
| 9.52 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_24_and_25_worker_heartbeats_then_stops_gracefully_on_sigterm` |
| 6.05 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_23_db_upgrade_creates_database_at_head` |
| 5.62 s | call | `tests/e2e/test_mcp_server.py::test_s5_ac_03_paper_trail_mcp_starts_standalone` |
| 5.39 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_23_db_upgrade_twice_is_a_no_op` |
| 5.38 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_show_prints_the_api_detail` |
| 3.14 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_dry_run_writes_nothing` |
| 3.13 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_show_unknown_id_exits_1` |
| 2.93 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_25_worker_stops_promptly_when_poll_exceeds_grace` |
| 2.91 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_inconsistent_import_is_recorded_for_review` |
| 2.90 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_import_then_list_over_http` |
| 2.87 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_import_then_list_over_http` |
| 2.85 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-line_total_cents]` |
| 2.84 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_dry_run_writes_nothing` |
| 2.84 s | call | `tests/e2e/test_mcp_server.py::test_s5_ac_03_mcp_refuses_an_unmigrated_database` |
| 2.77 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_malformed_json_exits_2` |
| 2.77 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-cashier]` |
| 2.74 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_inconsistent_import_is_recorded_for_review` |
| 2.72 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-total_cents0]` |
| 2.72 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-line_total_cents]` |
| 2.71 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_dry_run_of_a_consistent_file` |
| 2.71 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_missing_file_exits_2` |
| 2.71 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-total_cents1]` |
| 2.71 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_24_check_heartbeat_fails_when_file_is_missing` |
| 2.71 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_15_db_upgrade_stdout_is_json_including_alembic` |
| 2.70 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_show_prints_the_api_detail` |
| 2.70 s | setup | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-barcode]` |
| 2.70 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_dry_run_of_a_consistent_file` |
| 2.69 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_malformed_json_exits_2` |
| 2.69 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-total_cents1]` |
| 2.69 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_import_needs_a_migrated_database` |
| 2.68 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_show_unknown_id_exits_1` |
| 2.68 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-total_cents0]` |
| 2.68 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-cashier]` |
| 2.67 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_missing_file_exits_2` |
| 2.66 s | call | `tests/e2e/test_import_and_query.py::test_s1_ac_24_invalid_file_exits_2_naming_the_field[<lambda>-barcode]` |
| 2.51 s | call | `tests/e2e/test_cli_processes.py::test_s0_ac_15_serve_stdout_is_json_including_uvicorn` |
| 1.88 s | call | `tests/e2e/test_health_api.py::test_s0_ac_18_not_ready_when_database_unreadable` |
| 1.78 s | call | `tests/integration/test_database_health_probe.py::test_s0_ac_18_error_not_exception_when_unreadable` |
| 1.60 s | call | `tests/integration/test_database_health_probe.py::test_s0_ac_18_error_when_path_is_a_directory` |
| 1.02 s | call | `tests/integration/test_matching_sqlite.py::test_ac_c_03_and_s4_ac_02_memory_on_sqlite[mcp]` |

Every phase at about 2.7 s is one `python -m paper_trail` child process: the `migrated` fixture's `db upgrade` (setup) or the command under test (call). 36 of the top 40 phases are subprocess spawns; the other 4 are the database health-probe timeouts and one MCP-transport matching test.

### Time and test count per folder (plain run, setup + call + teardown)

| Folder | Tests | Time | Share of 260.9 s |
|---|---:|---:|---:|
| `tests/e2e` | 207 | 169.4 s | 65 % |
| `tests/contract` | 514 | 28.5 s | 11 % |
| `tests/unit` (all) | 839 | 37.4 s | 14 % |
| ↳ `tests/unit/agents` | 257 | 26.8 s | |
| ↳ `tests/unit/application` | 79 | 5.3 s | |
| ↳ `tests/unit/adapters` | 35 | 2.0 s | |
| ↳ `tests/unit/domain` | 259 | 1.8 s | |
| ↳ `tests/unit/*.py` (top level) | 130 | 1.2 s | |
| ↳ `tests/unit/tools` | 53 | 0.2 s | |
| ↳ `tests/unit/interfaces` | 26 | 0.1 s | |
| `tests/integration` | 158 | 14.2 s | 5 % |
| `tests/repo` | 270 | 11.6 s | 4 % |
| **Total** | **1,988** | **260.9 s** | |

### Per file, `tests/e2e`

| File | Tests | Time |
|---|---:|---:|
| `test_import_and_query.py` | 14 | 77.4 s |
| `test_cli_processes.py` | 7 | 31.8 s |
| `test_cli_process.py` | 22 | 12.9 s |
| `test_mcp_server.py` | 18 | 11.8 s |
| `test_cli_resume.py` | 12 | 8.5 s |
| `test_cli_review.py` | 12 | 7.6 s |
| `test_cli_items.py` | 10 | 7.3 s |
| `test_cli_process_mcp.py` | 7 | 6.0 s |
| `test_cli_tools.py` | 25 | 2.1 s |
| `test_health_api.py` | 7 | 2.0 s |
| `test_receipts_api.py` | 21 | 1.2 s |
| `test_openapi_contract.py` | 10 | 0.3 s |
| `test_errors_and_request_context.py` | 26 | 0.3 s |
| `test_spa.py` | 15 | 0.2 s |
| `test_cli_graph.py` | 1 | 0.0 s |

`test_import_and_query.py` plus `test_cli_processes.py` are 21 tests and 109.2 s: 42 % of the whole serial run.

### Other slow files (outside e2e, over 2 s)

| File | Tests | Time |
|---|---:|---:|
| `tests/contract/test_matching_tools.py` | 92 | 13.1 s |
| `tests/repo/test_import_contracts.py` | 34 | 10.6 s |
| `tests/unit/agents/test_resume.py` | 32 | 8.1 s |
| `tests/contract/test_tool_gateway.py` | 60 | 7.9 s |
| `tests/unit/agents/test_receipt_graph.py` | 38 | 7.5 s |
| `tests/unit/agents/test_matching_graph.py` | 32 | 6.7 s |
| `tests/unit/application/test_resolve_review.py` | 26 | 4.7 s |
| `tests/unit/agents/test_receipt_graph_tools.py` | 20 | 3.8 s |
| `tests/integration/test_database_health_probe.py` | 10 | 3.6 s |
| `tests/integration/test_matching_sqlite.py` | 4 | 2.6 s |
| `tests/integration/test_receipt_graph_sqlite.py` | 6 | 2.6 s |

### The `inprocess` / `mcp` transport parametrization

| Folder | `[inprocess]` tests | time | `[mcp]` tests | time |
|---|---:|---:|---:|---:|
| `tests/unit` | 75 | 3.8 s | 75 | 27.1 s |
| `tests/contract` | 76 | 3.1 s | 76 | 17.9 s |
| `tests/integration` | 5 | 1.2 s | 5 | 3.9 s |
| `tests/e2e` | 0 | | 1 | 0.2 s |
| **Total** | **156** | **8.1 s** | **157** | **49.1 s** |

The MCP twin of each test costs about 6x its in-process twin (a loopback uvicorn + MCP session per test). 49 s of the run (19 %) is the `[mcp]` half.

## (c) Test counts

**1,988 tests** (all passed, none skipped or xfailed). Per folder: unit 839, contract 514, repo 270, e2e 207, integration 158 (sub-folders of `tests/unit` in the table above).

## (d) Coverage

From `make test`: **97.75 %** total (branch coverage on): 4,917 statements, 78 missed; 640 branches, 45 partial. 108 files at 100 %. Gate is 90 %. Lowest files: `agents/json_values.py` 58 %, `adapters/ollama/http.py` 84 %, `application/products.py` 88 %.

## (e) Repository tests that assert documentation wording

Scope: tests that read `README.md`, `CONTRIBUTING.md`, `docs/**` (PLAN, plans, ADRs) or the `.env.example` templates and assert on their text. Prompt files under `src/paper_trail/agents/**/system.md` are product inputs, not documentation, and are excluded. **50 test items** (counting parametrized cases); together they run in 0.07 s, so removing them is about brittleness, not speed.

**KEEP** means the test guards a real property (config drift, security, or a command a user copies that must stay runnable). **REMOVE** means it only checks that some prose exists or is worded a certain way.

| Items | Test | Verdict | Reason |
|---:|---|---|---|
| 10 | `test_repo_hygiene.py::test_s0_ac_36_readme_quickstart_content[*]` | REMOVE | Substring presence in README prose; breaks on any rewording, no behaviour checked. |
| 3 | `test_repo_hygiene.py::test_s0_ac_36_readme_sections[*]` | REMOVE | Asserts heading names in README. |
| 1 | `test_repo_hygiene.py::test_s0_ac_36_readme_prerequisites_are_docker_make_git_only` | REMOVE | Wording of a prose section; the real "nothing on the host" property is enforced by the `test_makefile.py` S0-AC-02 checks. |
| 1 | `test_repo_hygiene.py::test_s0_ac_36_readme_lists_make_targets_in_a_table` | REMOVE | README table layout; `make help` is generated from the Makefile and already tested. |
| 1 | `test_repo_hygiene.py::test_s0_ac_36_mermaid_diagram_shows_layers_and_services` | REMOVE | Words inside a diagram; the layering itself is enforced by import-linter. |
| 1 | `test_repo_hygiene.py::test_s0_ac_36_and_39_readme_covers_the_windows_llm_host` | REMOVE | Substring checklist of README prose ("Firewall", "WSL2", ...). |
| 1 | `test_repo_hygiene.py::test_s0_ac_36_contributing_content` | REMOVE | Substring checklist of CONTRIBUTING prose. |
| 16 | `test_repo_hygiene.py::test_s0_ac_36_adrs_0001_to_0016_exist[1..16]` | REMOVE | Existence of numbered prose files, 16 items for one fact; ADRs are append-only documents, not behaviour. |
| 1 | `test_agent_core_repo.py::test_readme_documents_make_process` | REMOVE | Substring presence in README. |
| 1 | `test_agent_core_b_repo.py::test_readme_documents_the_tools_and_the_mcp_server` | REMOVE | Substring presence in README (exit code 5 is tested for real in `tests/e2e/test_cli_tools.py`). |
| 1 | `test_agent_core_b_repo.py::test_review_note_5_the_plan_is_owner_approved` | REMOVE | Asserts a status line in a planning document; process state, not product. |
| 1 | `test_agent_core_b_repo.py::test_review_note_5_the_e4_sample_path_matches_the_readme` | REMOVE | Cross-checks a path between two prose documents. |
| 1 | `test_agent_core_c_repo.py::test_review_note_2_plan_7_describes_the_mcp_server_as_built` | REMOVE | Asserts phrases present and absent in PLAN.md section 7. |
| 1 | `test_agent_core_c_repo.py::test_readme_documents_the_slice_c_commands` | REMOVE | Substring presence in README (the flags are exercised by the e2e CLI tests). |
| 1 | `test_agent_core_c_repo.py::test_the_e2_commands_are_runnable_as_written` | REMOVE | Regexes over one row of a planning document's acceptance table; the commands themselves are covered by e2e. |
| 1 | `test_repo_hygiene.py::test_s0_ac_39_windows_llm_host_steps_need_no_make` | KEEP | The Windows host has no bash, so a `make` line in the PowerShell blocks would be a broken instruction (drop the `ezwinports` wording assertion). |
| 2 | `test_makefile.py::test_s0_ac_39_llm_targets_match_the_windows_readme_commands[llm-up, llm-down]` | KEEP | Keeps the Windows copy-paste commands identical to what `make llm-up/down` run (drift guard on a runnable command). |
| 1 | `test_agent_core_repo.py::test_the_compose_run_macro_is_not_a_user_variable` | KEEP | The Makefile `RUN` macro assertion is real (a `RUN=` override breaks every recipe); the README line inside it could be dropped. |
| 1 | `test_agent_core_repo.py::test_the_receipts_dir_is_documented` | KEEP | `.env.example` default must equal the compose default mount; config drift, not prose. |
| 1 | `test_repo_hygiene.py::test_s0_ac_04_env_example_documents_every_setting_with_a_comment` | KEEP | Every `Settings` field has an entry in the config template (drift guard against the code). |
| 1 | `test_repo_hygiene.py::test_s0_ac_04_env_example_mentions_no_unknown_settings` | KEEP | The template names no setting that the code lacks (drift guard). |
| 1 | `test_repo_hygiene.py::test_s0_ac_04_env_example_holds_no_secrets` | KEEP | Security property of a committed file. |
| 1 | `test_compose.py::test_s0_ac_39_llm_env_example_documents_bind_addr_and_models` | KEEP | The LLM-host config template must name the bind address and the models `ollama-init` pulls; a missing value breaks deployment. |

Totals: **41 REMOVE** (15 test functions), **9 KEEP** (8 functions).

## (f) Tests that spawn subprocesses

Recorded with a throw-away pytest plugin (outside the repo, loaded through `PYTHONPATH`) that wraps `subprocess.Popen.__init__` and attributes each spawn to the running test (setup included). **58 tests, 77 spawns.** Threads (the in-process uvicorn behind `tests/mcp_server.py`, `tests/fakes/ollama_server.py`) are not subprocesses and are not listed.

### `python -m paper_trail` children (CLI start-up about 2.7 s each): 23 tests, 42 spawns

| File | Test | Spawns |
|---|---|---|
| `tests/e2e/test_import_and_query.py` | `test_s1_ac_24_dry_run_of_a_consistent_file` | `db upgrade`, `receipts import` |
| | `test_s1_ac_24_dry_run_writes_nothing` | `db upgrade`, `receipts import` |
| | `test_s1_ac_24_import_needs_a_migrated_database` | `receipts import` |
| | `test_s1_ac_24_import_then_list_over_http` | `db upgrade`, `receipts import` |
| | `test_s1_ac_24_inconsistent_import_is_recorded_for_review` | `db upgrade`, `receipts import` |
| | `test_s1_ac_24_invalid_file_exits_2_naming_the_field[*]` (5 cases) | `db upgrade`, `receipts import` (each) |
| | `test_s1_ac_24_malformed_json_exits_2` | `db upgrade`, `receipts import` |
| | `test_s1_ac_24_missing_file_exits_2` | `db upgrade`, `receipts import` |
| | `test_s1_ac_24_show_prints_the_api_detail` | `db upgrade`, `receipts import`, `receipts show` |
| | `test_s1_ac_24_show_unknown_id_exits_1` | `db upgrade`, `receipts show` |
| `tests/e2e/test_cli_processes.py` | `test_s0_ac_15_db_upgrade_stdout_is_json_including_alembic` | `db upgrade` |
| | `test_s0_ac_15_serve_stdout_is_json_including_uvicorn` | `serve` (long-running, `Popen`) |
| | `test_s0_ac_23_db_upgrade_creates_database_at_head` | `db upgrade` x2 |
| | `test_s0_ac_23_db_upgrade_twice_is_a_no_op` | `db upgrade` x2 |
| | `test_s0_ac_24_and_25_worker_heartbeats_then_stops_gracefully_on_sigterm` | `worker` x3 (`Popen` + `check-heartbeat`) |
| | `test_s0_ac_24_check_heartbeat_fails_when_file_is_missing` | `worker check-heartbeat` |
| | `test_s0_ac_25_worker_stops_promptly_when_poll_exceeds_grace` | `worker` (`Popen`) |
| `tests/e2e/test_mcp_server.py` | `test_s5_ac_03_mcp_refuses_an_unmigrated_database` | `mcp` |
| | `test_s5_ac_03_paper_trail_mcp_starts_standalone` | `db upgrade`, `mcp` (`Popen`) |

Of the 14 `test_import_and_query.py` tests, 9 check only argument or input errors (`invalid_file` x5, `malformed_json`, `missing_file`, `show_unknown_id`, `import_needs_a_migrated_database`) yet each pays 1 to 2 CLI start-ups; 13 of the 14 pay a separate `db upgrade` child in the `migrated` fixture.

### `lint-imports` children: 23 tests, 23 spawns

`tests/repo/test_import_contracts.py`: `test_s0_ac_06_real_tree_satisfies_every_contract` and all 22 cases of `test_s0_ac_06_forbidden_import_is_reported[*]` (10.6 s together).

### `tesseract` children (the real OCR adapter): 12 tests, 12 spawns

| File | Tests |
|---|---|
| `tests/integration/test_tesseract_ocr.py` | `test_a_blank_image_has_no_text_and_no_confidence`, `test_s2_ac_07_a_timeout_is_an_ocr_error`, `test_s2_ac_07_reads_store_name_and_total[COSTCO WHOLESALE, FRESHCO, SHOPPERS DRUG MART]` |
| `tests/e2e/test_cli_process.py` | `test_ac_a_02_the_same_photo_twice_is_a_duplicate`, `test_ac_a_10_a_directory_skips_non_images`, `test_ac_a_10_a_file_that_is_not_an_image_fails_with_exit_3`, `test_ac_a_10_the_llm_host_down_stops_the_batch` |
| `tests/e2e/test_cli_process_mcp.py` | `test_ac_b_05_an_image_is_read_over_mcp` |
| `tests/e2e/test_cli_resume.py` | `test_reprocess_reruns_a_stored_photo_from_its_ocr_text` |

These are fast (under 1 s each); the cost is the CLI ones above.

## Raw artifacts

Kept outside the repo, in the baseline agent's scratchpad: `make_test.log` (full `make test` output), `plain.log` (full `--durations=0` output), `junit.xml` (per-test times), `probe/spawns.json` (per-test subprocess argv). `coverage.xml` at the repo root was restored to its pre-baseline content after `make test` rewrote it.
