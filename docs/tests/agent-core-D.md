# Agent core slice D test plan: acceptance criteria to tests

Written by the test author on 2026-11-06, before any slice D implementation exists. Sources of truth: `docs/plans/agent-core.md` §3.3, §4 and §5, `docs/PLAN.md` §5 (S3-AC-04, S3-AC-10, S3-AC-12), §6 (S4-AC-04, S4-AC-05) and §7 (S5-AC-01, S5-AC-04, S5-AC-05), ADR-0008, ADR-0009, ADR-0011 and ADR-0013, the slice C review notes 1-5 and the test-suite cleanup review notes (a)-(d). Suites, markers, fakes and Make targets are the earlier slices' (`docs/tests/agent-core-A.md` to `agent-core-C.md`, `docs/tests/test-suite-cleanup.md`).

**No test touches a real model.** The LLM is `ScriptedLlm` (unit, contract, integration) or the scripted fake Ollama on 127.0.0.1 (CLI); embeddings are `FakeEmbeddings` or the fake Ollama's `/api/embed`. The autouse loopback guard still fails any other connection. Nothing here runs `paper-trail process` or `llm check` against the LLM host.

While developing run the targeted modules and `make test-fast`; `make ci` once at the end (owner decision 2026-11-06).

## How the tests fail before the implementation

`make test-fast` on 2026-11-06 with only the test author's changes: 1124 passed, 190 failed, 215 errors.

- **`AgentDeps(images=...)`**: `tests/agent_harness.py` passes the image store to `AgentDeps` (the vision attempt reads the photo through it), so every test that builds a harness fails with `TypeError: AgentDeps.__init__() got an unexpected keyword argument 'images'` (367 of the failures and errors, in every graph, tool and use-case suite). This is the implementer's first step, as `CorrectLineItem` was in slice C.
- `tests/unit/agents/test_enrichment_prompt.py` fails at collection (`EnrichmentOutput` in `paper_trail.agents.schemas`).
- On their assertions: `GraphSettings.enrichment`, `RunConfig.enrichment` and the trace's `config.enrichment`, the effective vision limit, `ProductRepository.update` (SQLite), review note 4 (6 cases), review note 5 (5 flags), the lazy version (cleanup note c).

End to end and repository suites (`pytest -n 6 tests/e2e tests/repo`): 409 passed, 5 failed (the graph snapshot, two slice D CLI tests, the real `process` process now expecting enrichment, the lazy version), 30 errors (the CLI `tools` and MCP server suites build their registry through the harness). The other CLI suites still pass with the slice D stages switched off by `configure_cli`.

`mypy` reports 13 errors, all names that do not exist yet (and one tuple comparison that resolves when `NODE_NAMES` grows). `make lint` passes, import contracts included.

Already green: the worker SIGTERM test with the short poll (cleanup note d: it covers existing behaviour), the repository test of the worker cap (the test author capped the workers, see below), `FakeProductRepository.update`.

## Suites and how they run

| Suite | Slice D files | Runs with |
|---|---|---|
| Unit (fakes) | `tests/unit/agents/test_vision_fallback.py`, `test_enrichment_graph.py`, `test_enrichment_prompt.py`, `tests/unit/test_config_agent_core_d.py`; additions to `test_matching_graph.py` (review notes 2, 3), `test_resume.py` (note 1), `test_graph_shape.py`, `test_tracing.py`, `test_transport_equivalence.py`, `tests/unit/tools/test_registry.py`, `tests/unit/test_run_config.py`, `tests/unit/test_cli_agent_core.py` (note 5), `tests/unit/test_log_privacy.py`, `tests/unit/adapters/test_ollama_embeddings.py` (note 4), `test_trace_document.py`, `tests/unit/test_worker_runner.py` (cleanup note d) | `make test-fast`, `make test` |
| Contract | `tests/contract/test_enrichment_tools.py` (in-process and MCP, each over the fake and the SQLite unit of work), `tests/contract/test_repositories_agent_core_d.py` (fake and SQLite); additions to `test_matching_tools.py` (note 1) | `make test-fast`, `make test` |
| Integration | `tests/integration/test_vision_enrichment_sqlite.py` (application tables, FTS, sqlite-vec and `CheckpointSaver` in one file, two "processes") | `make test-fast`, `make test` |
| End to end | `tests/e2e/test_cli_vision_enrichment.py`; `test_cli_graph.py` snapshot; `test_cli_processes.py` (the real `process` process now runs with the production defaults, enrichment included); `test_cli_startup.py` (cleanup note c) | `make test` |
| Repository | `tests/repo/test_agent_core_d_repo.py` (worker cap) | `make test` |

Graph suites run in-process; `test_transport_equivalence.py` gains the scenario `vision_and_enrichment` (both text attempts fail, the vision attempt reads the photo, three products are enriched), so S5-AC-04 covers the slice D graph, and `test_enrichment_tools.py` covers `save_enrichment` over both transports.

### Test support written by the test author

- **`tests/agent_harness.py`**: `AgentDeps(..., images=images)`. `agent_settings(...)` now defaults to `max_vision_attempts=0` and gains `enrichment: bool = False`: the slice A-C suites keep pinning exactly the calls and events of their stages; slice D tests pass `max_vision_attempts=1` (the production default) and `enrichment=True`.
- **`tests/e2e/agent_cli.py`**: `configure_cli(..., slice_d=False)` sets `PAPER_TRAIL_GRAPH__MAX_VISION_ATTEMPTS=0` and `PAPER_TRAIL_GRAPH__ENRICHMENT=false` unless `slice_d=True` (production defaults), for the same reason.
- **`tests/extractions.py`**: `enrichment(...)` (an `EnrichmentOutput` answer as JSON) and `COSTCO_ENRICHMENTS` (one answer per line of the default receipt: eggs and milk `dairy_eggs`, sourdough `bakery`).
- **`tests/fakes/repositories.py`**: `FakeProductRepository.update`.
- **`tests/fakes/tools.py`**: `RecordingToolGateway` (records each call's role, tool and payload; the trace keeps outputs only).
- **`tests/pans.py`**: `UUID` (the checkpoint-id exclusion of the card-number scan), moved from `test_receipt_graph_sqlite.py` so both SQLite scans share it.

### Changes to existing tests

- `test_receipt_graph.py`: `test_ac_a_07_slice_a_never_routes_to_vision` removed (superseded by `test_vision_fallback.py`); the docstring pin says vision is off in that suite.
- `test_graph_shape.py`, `e2e/test_cli_graph.py`: nodes `extract_vision`, `enrich`, `commit_enrichment`; edges `supervisor -> extract_vision -> validate`, `commit_matches -> enrich | END`, `enrich -> commit_enrichment -> END`; `NODE_NAMES` in graph order.
- `test_resume.py`: `test_a_breakpoint_after_persist_reports_the_saved_status` and `test_ac_c_05_a_drop_during_matching_resumes_matching_only` now expect PROCESSING/MATCHING and PROCESSING/WAITING_FOR_GPU (review note 1; this reverses slice C decision 7).
- `test_run_config.py`: the vision limit is the configured one; `prompt_versions` has `enrichment`; `enrichment` is recorded.
- `test_trace_document.py`: `TraceConfig` gains `enrichment` (the committed `trace.schema.json` must be regenerated).
- `test_registry.py`: `SAMPLE_PAYLOADS["save_enrichment"]`, `SLICE_D_TOOLS`; the generated matrix checks its allowlist (supervisor only).
- `test_tracing.py`: node and tool role maps gain the slice D names.
- `e2e/test_cli_processes.py`: the real `process` process answers three enrichments (4 chat calls).

### Done by the test author (configuration and documentation only)

- **Worker cap**: the Makefile's `test` and `test-fast` used `pytest -n auto` (8 workers on this host). Both now use `-n $(PYTEST_WORKERS)`, `PYTEST_WORKERS := 6`, defined once with the reason in a comment (the host's cores also serve the owner's media services). CI calls the Make targets, so it inherits it; `CONTRIBUTING.md` says `-n 6`. Guarded by `tests/repo/test_agent_core_d_repo.py`.
- **Cleanup note (a)**: `docs/tests/agent-core-B.md` and `agent-core-C.md` no longer say the graph and matching suites run over both transports; they point to `test_transport_equivalence.py` and the contract suites.
- **Cleanup note (b)**: ADR-0002 describes the `paper_trail.bootstrap` package (`__init__` for the application core, `agents` for the agent core) instead of `bootstrap.py`.

## Acceptance criteria to tests

| AC | Tests |
|---|---|
| **AC-D-01** = S3-AC-04 in full | `unit/agents/test_vision_fallback.py::test_s3_ac_04_two_text_attempts_then_one_vision_attempt_then_review` (3 calls, `vision_attempts == 1`, review OPEN with the last attempt's issues, NEEDS_REVIEW, routes `extract_text, extract_vision, review`, parked not failed), `::test_ac_d_01_the_vision_request_uses_the_vision_model_and_carries_the_photo`, `::test_ac_d_01_the_vision_attempt_carries_the_latest_issues`, `::test_ac_d_01_without_feedback_*`, `::test_ac_d_01_without_examples_*`, `::test_ac_d_01_a_vision_read_that_adds_up_is_saved_and_matched`, `::test_a_vision_answer_that_never_validates_ends_in_extraction_failed`, `::test_every_vision_attempt_allowed_is_made`, `::test_ac_d_01_the_trace_names_the_vision_model_and_never_holds_the_image`; `e2e/test_cli_vision_enrichment.py::test_ac_d_01_*` (the vision model and the stored photo as base64 on the wire, exit 0, trace totals), `::test_no_vision_skips_the_vision_attempt`; `unit/agents/test_transport_equivalence.py` (`vision_and_enrichment`) |
| AC-D-01 with S3-AC-10 | `unit/agents/test_vision_fallback.py::test_a_gpu_drop_during_the_vision_attempt_resumes_with_the_vision_attempt_only`; `integration/test_vision_enrichment_sqlite.py::test_ac_d_01_a_gpu_drop_at_the_vision_attempt_resumes_in_a_new_process` |
| **AC-D-02** = S4-AC-05 | Schema: `unit/agents/test_enrichment_prompt.py::test_s4_ac_05_the_schema_offers_only_the_category_enum`, `::test_s4_ac_05_a_category_outside_the_enum_is_rejected`. Memory: `contract/test_enrichment_tools.py::test_s4_ac_05_a_category_correction_for_the_alias_overrides_the_model`, `::test_only_a_category_correction_for_this_alias_and_store_counts`; `unit/agents/test_enrichment_graph.py::test_s4_ac_05_*` (an invented category leaves the product unenriched; the correction wins over the model) |
| AC-D-02 "only products created in this receipt" | `unit/agents/test_enrichment_graph.py::test_s4_ac_04_the_next_identical_receipt_hits_the_alias_and_enriches_nothing`, `::test_two_lines_of_one_new_product_enrich_it_once`, `::test_a_product_found_by_the_vector_index_is_not_enriched`, `::test_with_enrichment_off_the_stage_does_not_run` |
| S4-AC-04 in full ("enriches it") | `unit/agents/test_enrichment_graph.py::test_s4_ac_04_new_products_are_enriched_with_their_vectors`, `::test_s4_ac_04_the_next_identical_receipt_*`; `contract/test_enrichment_tools.py::test_save_enrichment_*`; `integration/test_vision_enrichment_sqlite.py::test_s4_ac_04_enriched_products_are_searchable_and_their_vectors_replaced`; `contract/test_repositories_agent_core_d.py`; `e2e/test_cli_vision_enrichment.py::test_new_products_are_enriched_and_shown_on_the_terminal` |
| Enrichment graph shape, order, trace | `unit/agents/test_enrichment_graph.py::test_enrichment_runs_after_matching_and_the_saves_come_after_every_request`, `::test_the_enrichment_requests_name_each_new_products_line`, `::test_without_examples_the_tag_says_so`, `::test_commit_enrichment_*`, `::test_the_enrichment_llm_events_belong_to_the_enrichment_agent`, `::test_a_low_confidence_answer_leaves_the_product_unenriched`, `::test_a_gpu_drop_during_enrichment_keeps_the_receipt_and_resumes_enrichment_only`; `unit/agents/test_graph_shape.py` |
| **AC-D-03** | `unit/agents/test_vision_fallback.py::test_ac_d_03_a_text_input_never_routes_to_vision`, `::test_ac_d_03_a_reprocessed_photo_may_still_use_vision`; `e2e/test_cli_vision_enrichment.py::test_ac_d_03_a_text_file_never_gets_a_vision_attempt` |
| **AC-D-04** = S3-AC-12 | Manual, real model, during the Sunday runs (agent-core plan §6): a clean photo completes; a damaged one (torn, total covered) ends in review. Not automated: it needs the real models |
| S5-AC-01 for `save_enrichment` | `unit/tools/test_registry.py` (generated matrix: supervisor only) |
| S5-AC-04 for slice D | `unit/agents/test_transport_equivalence.py` (`vision_and_enrichment`); `contract/test_enrichment_tools.py` (both transports) |
| S5-AC-05 for slice D | `unit/agents/test_enrichment_graph.py::test_s5_ac_05_save_enrichment_called_by_the_enrichment_agent_is_denied` |
| AC-A-04 with vision and enrichment | `integration/test_vision_enrichment_sqlite.py::test_ac_a_04_*` (no card number in any column, checkpoints included, or any request) |
| AC-A-14 with vision and enrichment | `unit/test_log_privacy.py::test_ac_a_14_slice_d_vision_and_enrichment_log_no_content` |
| Settings | `unit/test_config_agent_core_d.py`; `unit/test_run_config.py`; `unit/test_cli_agent_core.py::test_slice_d_with_flags_*`; the generic `.env.example` drift test picks up `GRAPH__ENRICHMENT` |
| Slice C review note 1 (MATCHING stage) | `unit/agents/test_resume.py::test_a_breakpoint_after_persist_reports_the_saved_status`, `::test_review_note_1_the_receipt_stays_matching_until_commit_matches`, `::test_ac_c_05_a_drop_during_matching_resumes_matching_only`; `contract/test_matching_tools.py::test_review_note_1_*`; `integration/test_vision_enrichment_sqlite.py::test_review_note_1_a_drop_during_matching_is_processing_on_disk` |
| Slice C review note 2 (keep a person's links) | `unit/agents/test_matching_graph.py::test_review_note_2_a_line_a_person_linked_in_the_review_is_not_matched_again` |
| Slice C review note 3 (`line_position`) | `unit/agents/test_matching_graph.py::test_review_note_3_an_answer_about_another_line_is_not_a_confirmation` |
| Slice C review note 4 (embedding elements) | `unit/adapters/test_ollama_embeddings.py::test_review_note_4_*` |
| Slice C review note 5 (`--resume` flags) | `unit/test_cli_agent_core.py::test_review_note_5_resume_refuses_flags_that_would_change_the_run` |
| Cleanup note (c) (lazy version) | `e2e/test_cli_startup.py::test_the_version_is_looked_up_only_when_asked_for`; `unit/test_cli.py::test_s0_ac_26_*` still pins the value |
| Cleanup note (d) (short-poll SIGTERM) | `unit/test_worker_runner.py::test_s0_ac_25_sigterm_with_the_normal_short_poll_stops_promptly` |
| Worker cap | `repo/test_agent_core_d_repo.py::test_parallel_test_runs_use_at_most_six_workers` |
| Cleanup notes (a), (b) | Documentation only; checked in review (no wording tests) |

Out of slice D, not tested here: the unit price per base unit (`unit_price_per_base_unit`, PLAN 6 "computes size/unit price") has no stored column; it is computed when prices are read (Stage 6 search and price pages). Search by an enrichment tag through the HTTP API (S6-AC-02) is Stage 6; slice D only shows the FTS index follows the tags.

## Details the plans left open, pinned by the tests

### Settings

- `GraphSettings.enrichment: bool = True` (`PAPER_TRAIL_GRAPH__ENRICHMENT`). No CLI flag.
- `GRAPH__MAX_VISION_ATTEMPTS` (default 1) is effective: `graph_limits(settings).max_vision_attempts` equals it. `--no-vision` (slice A) sets it to 0.
- `bootstrap.agents.run_config` records `enrichment` and `prompt_versions["enrichment"]`; `TraceConfig` (and the committed `trace.schema.json`) gains `enrichment: bool`.

### Graph

- `NODE_NAMES = ("read", "extract_text", "extract_vision", "validate", "supervisor", "review", "persist", "match", "commit_matches", "enrich", "commit_enrichment")`.
- `supervisor -> extract_vision -> validate`. The supervisor routes there with `route()` unchanged (text attempts used up, vision attempts left), and with a vision limit of 0 for a receipt without `image_path` (a text input, AC-D-03). A reprocessed photo keeps its image, so it may use vision.
- `commit_matches -> enrich -> commit_enrichment -> END` when `GRAPH__ENRICHMENT` is on and this run's `save_matches` linked at least one line with source `new`; otherwise `commit_matches -> END`.
- Agents: `extract_vision` and `enrich` EXTRACTION and ENRICHMENT; `commit_enrichment` SUPERVISOR.

### Vision attempt

- `AgentDeps` gains `images: ImageStore`; the photo is read with `ImageStore.open(image_path)` (not through a tool: image bytes have no place on the MCP wire).
- Request: `settings.ollama.vision_model`, temperature 0, the extraction prompt's tag (`extraction/v1`, `+noex` without examples), the same messages as a text retry (system, example pairs, final user message with the feedback and the OCR text in `<receipt_data>`); only the final user message has `images`, exactly the stored photo's bytes. Feedback: the latest attempt's issues, or the generic line with feedback off.
- Answer validated as `ExtractionOutput`; `vision_attempts += 1` whether it parses or not. An unparsed last attempt is EXTRACTION_FAILED, as in slice A.
- Trace: `TracingLlm` records, for a message with images, `"images": [<sha256 hex>, ...]`; no other message entry has the key; no image bytes or base64 anywhere in the trace.

### Enrichment

- `EnrichmentOutput` (`agents/schemas.py`, `extra="forbid"`): `canonical_name: str` (not blank), `brand: str | None`, `size_value: str | None` (positive decimal string; a JSON number is refused), `size_unit: Unit | None` (given exactly when `size_value` is), `category: Category`, `tags: list[str]` (at most 8, each non-empty and lowercase), `confidence: float` 0..1. Suggested, not pinned: a `ProductEnrichment` payload in `application/payloads.py` without `confidence` (the tool's input), extended by `EnrichmentOutput`, as `ExtractedReceipt`/`ExtractionOutput`.
- Prompt `agents/prompts/enrichment/v1/{system.md, user.md, schema.json}`: `schema.json` equals the Pydantic schema; `category` is an enum; the system prompt states the data rule and lists every category value; `user.md` has `{ocr_text}` inside `<receipt_data>`.
- `enrich`: the products to enrich are the distinct product ids of this run's `new` lines, in line order; the first such line is the source line. One request each: text model, temperature 0, tag `enrichment/v1` (`+noex`), the source line's raw text and description inside `<receipt_data>`. An answer that never validates, or whose confidence is below `GRAPH__MIN_CONFIDENCE`, leaves the product as it is (not retried). `LlmUnavailableError` propagates (checkpoint kept).
- `commit_enrichment`: one `save_enrichment` per enriched product, same order, after every enrichment request (the text model's calls, then the embedding model's: one model switch, the measured ~7 s, instead of one per product).
- The receipt is COMPLETED/DONE once `save_matches` ran; enrichment changes products only. A GPU drop during enrichment leaves the receipt COMPLETED/DONE; `--resume` continues after `commit_matches` (`resumed_from = "commit_matches"`).

### Tools and ports

- `save_enrichment {product_id: int, alias: str, store_key: str | null, enrichment: {canonical_name, brand, size_value, size_unit, category, tags}}` -> `{product: ProductOut, category_source: "model" | "memory"}`; supervisor only. The alias is normalized by the tool. Category memory: the newest CATEGORY correction for exactly (alias, store_key) replaces the model's category. One unit of work: `ProductRepository.update` (name masked), and the vector replaced by the embedding of `normalize_alias(" ".join([canonical_name, *tags]))` (one call, one text, before the write). NotFoundError for an unknown product; a failed embedding writes nothing and stays `LlmUnavailableError` over MCP. Idempotent. Trace summary: the stored category, plus `memory` when memory chose it.
- `ProductRepository.update(product) -> Product`: replaces name, brand, size, category and tags; keeps id, `created_at`, aliases, vector and prices; NotFoundError (nothing changed) for an unknown id; the FTS index follows (SQLite triggers).

### Slice C review notes

1. `save_receipt` stores an accepted draft (adds up, or forced by a person) as PROCESSING/MATCHING (its output says so); `save_matches` sets COMPLETED/DONE. `GraphOutcome.status` is COMPLETED at the end, PROCESSING for a pause after `persist` or `match`. A GPU drop during matching therefore leaves the receipt PROCESSING/WAITING_FOR_GPU (S3-AC-10 as written), reversing slice C decision 7.
2. `match` skips the lines a person named a product for in the review answer (`DraftPayload.product_names`, linked by `persist`): no tool call, no decision, the link kept.
3. A confirmation whose `line_position` is not the asked line's position counts as "different" (a new product), not retried.
4. `OllamaEmbeddings`: every vector element must be a JSON number; strings (even numeric), null, containers and booleans are `LlmOutputError`; integers are accepted and returned as floats.
5. `process --resume` refuses `--prompt-version`, `--no-examples`, `--no-feedback`, `--max-text-attempts` and `--no-vision` (exit 2, the message names the flag) before opening anything. `--run`, `--transport`, `--verbose` and `--no-color` stay allowed.

### Test-suite cleanup notes

- (c) `paper_trail.__version__` resolves `importlib.metadata.version("paper-trail")` on first access (module `__getattr__`), and the CLI's `version` command reads it there, not at import: importing the CLI and running `--help` performs no lookup.
- (d) Unit-level: a real `WorkerRunner` with a 0.1 s poll stops with exit 0 on SIGTERM, promptly, heartbeat written.

## Decisions for the owner or orchestrator to confirm

1. **`GRAPH__ENRICHMENT` (default on)** is new: it lets an experiment arm skip the enrichment calls, and lets the earlier suites stay exact. No `process` flag was added (the plan lists none).
2. **The harness and the CLI suites default to the slice D stages off** (`agent_settings`, `configure_cli`); slice D tests and the real-process test turn them on. The alternative, re-scripting every earlier test with vision and enrichment calls, would make each of them about slice D.
3. **`save_enrichment` takes `alias` and `store_key`** and applies the category memory itself (PLAN 7 wrote `save_enrichment(product_id, enrichment) -> ProductOut`): memory is enforced where the write happens, whatever a node sends, and the enrichment role keeps PLAN 7's allowlist (no new read tool). Its output is `{product, category_source}`.
4. **Category memory rarely fires today**: `items correct` always records a PRODUCT_LINK correction with the CATEGORY one for the same alias and store, so the next such line is linked by memory and never new. A category-only correction (e.g. `items correct --category` without a product target) would make it useful; not added (it changes the slice C command contract). `items correct --product-id N --category C` still leaves product N's category unchanged (slice C pin kept).
5. **A low-confidence enrichment is not saved** (same threshold as the confirmation, `GRAPH__MIN_CONFIDENCE`), rather than stored with a doubtful category.
6. **The vision attempt reuses the extraction prompt** (with the OCR text and the feedback) instead of a separate vision prompt: one prompt to version, and the model sees both the photo and what OCR read.
7. **Enrichment does not keep the receipt in an ENRICHING stage**: the receipt's contents and links are final after `save_matches`; a GPU drop during enrichment leaves products unenriched until `--resume`, not the receipt unfinished. `ProcessingStage.ENRICHING` stays for the Stage 2 progress events.
8. **`--resume` also refuses `--no-vision`** (review note 5 listed four flags; this one changes the run the same way).
