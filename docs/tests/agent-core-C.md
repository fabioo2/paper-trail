# Agent core slice C test plan: acceptance criteria to tests

Written by the test author on 2026-11-06, before any slice C implementation exists. Sources of truth: `docs/plans/agent-core.md` §3.2, §4.1 and §6 (E2), `docs/PLAN.md` §1.3, §3.4, §5 (S3-AC-08, S3-AC-10) and §6 (S4-AC-01..08), ADR-0002, ADR-0004, ADR-0008, ADR-0009 and ADR-0013, and the slice B review notes. Suites, markers, fakes and Make targets are the Stage 0, Stage 1, slice A and slice B ones (`docs/tests/agent-core-A.md`, `docs/tests/agent-core-B.md`).

**No test touches a real model.** The LLM is `ScriptedLlm` (unit, contract, integration) or the scripted fake Ollama on 127.0.0.1 (CLI). Embeddings are `FakeEmbeddings` (unit, contract, integration), respx (adapter), or the fake Ollama's new `/api/embed` (CLI). The autouse loopback guard in `tests/conftest.py` still fails any other connection. Nothing here runs `paper-trail process` or `llm check` against the LLM host.

Run everything with `make test` (or `make test-unit`, `make test-integration`); `make lint` and `make typecheck` cover the new test code too.

## How the tests fail before the implementation

25 modules fail at collection, because they import names that do not exist yet:

- `paper_trail.application.corrections` (`CorrectLineItem`): `tests/agent_harness.py` builds it, so every graph suite that uses the harness fails here (17 modules);
- `paper_trail.adapters.ollama.embeddings` (`OllamaEmbeddings`) and `paper_trail.ports.embeddings` (`EmbeddingPort`);
- `ItemLink` in `paper_trail.domain.models`, `GENERIC_ALIASES` and `is_generic_alias` in `paper_trail.domain.rules.normalization`;
- `MatchingSettings` in `paper_trail.config`;
- `MatchDecision` in `paper_trail.agents.schemas`, `NODE_NAMES` in `paper_trail.agents.graph`;
- `with_transport` in `paper_trail.interfaces.cli.process`.

The fakes import `ItemLink` for typing only (`TYPE_CHECKING`), so the suites that do not use the harness still collect. Of those, 7 tests fail on their assertions (verified 2026-11-06 with `pytest --continue-on-collection-errors -m "not e2e"`: 1296 passed, 7 failed, 25 collection errors):

- review note 4: the https default port, and the two text-only results (`tests/unit/adapters/test_mcp_gateway.py`);
- review note 3: one `tools.model_copy(` under `interfaces/cli`;
- the README does not yet document the slice C commands;
- `run_config` does not yet record the matching prompt and the embedding model.

In the end-to-end suite every slice C test fails (22 in `test_cli_items.py` and `test_cli_resume.py`, plus the `graph` snapshot); the usage-error tests also assert that the refusal is not Typer's "No such option/command", so they cannot pass just because the flag does not exist yet.

Two repository tests already pass because the test author made the documentation changes they check (review note 2 in `docs/PLAN.md` §7; the runnable E2 commands in `docs/plans/agent-core.md`).

The implementer's first step is the missing names; with them the whole suite collects again.

## Suites and how they run

| Suite | Slice C files | Runs with |
|---|---|---|
| Unit (fakes) | `tests/unit/agents/test_matching_graph.py`, `tests/unit/agents/test_resume.py`, `tests/unit/agents/test_matching_prompt.py`, `tests/unit/application/test_correct_line_item.py`, `tests/unit/adapters/test_ollama_embeddings.py` (respx), `tests/unit/domain/test_agent_core_c_domain.py`, `tests/unit/test_config_agent_core_c.py`; additions to `test_mcp_gateway.py`, `test_run_config.py`, `test_cli_agent_core.py`, `test_registry.py`, `test_receipt_graph_tools.py`, `test_graph_shape.py` | `make test`, `make test-unit` |
| Contract | `tests/contract/test_embedding_port.py` (fake and Ollama adapter), `tests/contract/test_matching_tools.py` (in-process and MCP, each over the fake and the SQLite unit of work with the real sqlite-vec index), `tests/contract/test_repositories_agent_core_c.py` (fake and SQLite) | `make test`, `make test-unit` |
| Integration | `tests/integration/test_matching_sqlite.py` (application tables, sqlite-vec and `CheckpointSaver` in one file, two "processes"; in-process only, MCP covered by `test_transport_equivalence.py`) | `make test`, `make test-integration` |
| End to end | `tests/e2e/test_cli_resume.py`, `tests/e2e/test_cli_items.py`; `test_cli_graph.py` updated | `make test` |
| Repository | `tests/repo/test_agent_core_c_repo.py` | `make test` |

Since the test-suite cleanup `test_matching_graph.py`, `test_resume.py` and `test_matching_sqlite.py` run in-process only; S5-AC-04 for the slice C graph is covered by `tests/unit/agents/test_transport_equivalence.py` (its scenarios run matching, resume and review over both gateways), and the tools themselves by `tests/contract/test_matching_tools.py` over both.

### Test support written by the test author

- **`tests/fakes/embeddings.py`**: `FakeEmbeddings(table=None, *, error=None)` implements `EmbeddingPort`. A text in the table gets its vector; any other text a deterministic unit vector seeded by its hash (two such vectors are about 1.0 apart in cosine distance, so an unknown line is never "similar" by accident). `requests`, `calls` and `texts` record what was embedded; an empty batch is no call. `error` is raised by every call (a GPU that dropped). `basis(axis)` and `at_distance(d, axis=0, toward=1)` build vectors at an exact cosine distance from each other, to put a line below, inside or above the thresholds.
- **`tests/fakes/ollama_server.py`**: `POST /api/embed` answers with `FakeEmbeddings` vectors (`FakeOllama(..., embeddings=table)`), recorded in `embed_requests`; `fake.calls` still counts chat requests only. `tests/e2e/agent_cli.py`'s `start(...)` passes `embeddings` through.
- **`tests/fakes/repositories.py`**: `FakeReceiptRepository.link_items`, `FakePriceRepository.reassign`, and `replace_contents` now drops the replaced lines' price observations, as SQLite's foreign key cascade does (held to the same contract).
- **`tests/agent_harness.py`**: `build_harness(..., embeddings=None)` wires `ToolServices(embeddings=...)` and `CorrectLineItem(uow_factory, clock)`; `Harness` gains `embeddings`, `checkpointer`, `correct`, and `process_image(label, *, reprocess, interrupt_after)`, `process_text(text, name, *, interrupt_after)`, `resume(receipt_id)`, `line`, `match_sources`, `product`, `product_ids`, `aliases`, `find_by_alias`, `price_history`, `has_vector`, and `latest_correction(subject, kind, store_key)`. `agent_settings(..., auto_link_max_distance=0.15, review_max_distance=0.30)`.
- **`tests/extractions.py`**: the default receipt's aliases (`EGGS_ALIAS`, `MILK_ALIAS`, `SOURDOUGH_ALIAS`), the Walmart spelling of the eggs (`KIRKLAND_EGGS`), `one_line_extraction`, `mixed_lines_extraction` (eggs, a deposit, a discount and a generic "PRODUCE" line) and `match_decision`.
- **`tests/mcp_server.py`**: `serve_text_only_mcp(text)`, a minimal foreign MCP server whose calls succeed with text content and no `structuredContent` (review note 4); `serve_mcp` now shares its serving code.

### Changes to existing tests

- Slice A step sequences gain the matching tail: `test_receipt_graph.py` (`MATCHING_STEPS`: on a fresh database, `product_lookup` then `product_similarity_search` per line, then `save_matches`), `test_resolve_review.py` (the resumed run ends with `save_matches`).
- `test_graph_shape.py` and `e2e/test_cli_graph.py`: the snapshot gains `match`, `commit_matches` and the edges `persist -> match -> commit_matches -> END`; `NODE_NAMES` equals the node set.
- `test_registry.py`: `SAMPLE_PAYLOADS` for the three new tools, and the registry must register them (`SLICE_C_TOOLS`). The matrix itself is generated, so their allowlists are checked without other changes.
- `test_receipt_graph_tools.py`: the out-of-role matrix gains `product_lookup` as SUPERVISOR, `product_similarity_search` as ENRICHMENT and `save_matches` as MATCHING.
- `test_config_agent_core_b.py`: the 30 s MCP timeout default and the 5 s environment value are gone (review note 1).
- `test_cli_agent_core.py`: the slice C `process` flags and the `items` command.
- `test_run_config.py`: the matching prompt version and the embedding model are recorded.
- `test_mcp_gateway.py`: review note 4.

### Documentation changed by the test author

- `docs/PLAN.md` §7 (review note 2): the goal and the `interfaces/mcp/server.py` bullet describe the as-built server (the SDK's low-level `Server`, `create_mcp_app`) and say that `process --transport mcp` serves its own loopback server, so `make mcp-up` is for `tools --transport mcp` and external clients only.
- `docs/plans/agent-core.md` §6 E2: `items correct` needs `--run e2` (the receipts are in the run's database), `<pos>` is the 0-based stored position, and the resume step is `make pt ARGS="process --run e2 --resume <id>"`, because `make process` always passes a PATH and `--resume` takes none (the same reason E4 uses `make pt`).

## Acceptance criteria to tests

| AC | Tests |
|---|---|
| **AC-C-01** = S4-AC-01 | `unit/agents/test_matching_graph.py::test_s4_ac_01_an_alias_hit_calls_neither_embeddings_nor_the_model` (zero embedding calls, no `matching/v1` request, no `product_similarity_search` event, the same products, two prices each); `contract/test_matching_tools.py::test_lookup_*` |
| AC-C-01 = S4-AC-02 | `unit/agents/test_matching_graph.py::test_s4_ac_02_close_embeddings_link_two_stores_to_one_product` (VECTOR link, price history with `costco` and `walmart`, a MATCHED alias for the Walmart spelling); `integration/test_matching_sqlite.py::test_ac_c_03_and_s4_ac_02_memory_on_sqlite` (through sqlite-vec) |
| AC-C-01 = S4-AC-03 (C-stretch) | `unit/agents/test_matching_graph.py::test_s4_ac_03_same_links_after_exactly_one_confirmation_with_the_top_candidates` (one request, text model, temperature 0, `matching/v1`, the three nearest candidates and the line inside `<receipt_data>`, the fourth absent, MATCHING agent, source `llm`); `::test_s4_ac_03_anything_but_a_confident_same_creates_a_new_product` (different, an id not offered, low confidence, unparsed); `::test_a_candidate_within_auto_link_wins_without_asking`; `::test_thresholds_come_from_settings`; `unit/agents/test_matching_prompt.py` |
| AC-C-01 = S4-AC-04 | `unit/agents/test_matching_graph.py::test_s4_ac_04_a_first_receipt_creates_a_product_per_line` (named after the description, vector stored, aliases, prices), `::test_s4_ac_04_a_line_far_from_every_product_is_new`, and the alias hit of the next identical line in `::test_s4_ac_01_*`. "Enriches it" is slice D |
| AC-C-01 = S4-AC-06 | `unit/application/test_correct_line_item.py` (relink as MEMORY, price observation moved, PRODUCT_LINK correction, USER_CORRECTION alias, the next receipt via memory with no vector search, new product with category, category memory, masking, errors); `contract/test_repositories_agent_core_c.py::test_reassign_*`; `e2e/test_cli_items.py` (CLI form) |
| AC-C-01 = S4-AC-07 | `unit/agents/test_matching_graph.py::test_s4_ac_07_every_linked_line_has_its_source_and_others_are_never_matched` (DEPOSIT, DISCOUNT and a generic alias get no tool call and no link); `contract/test_repositories_agent_core_c.py::test_link_items_*`; `unit/domain/test_agent_core_c_domain.py` (stop-list) |
| AC-C-01 = S4-AC-08 | `unit/adapters/test_ollama_embeddings.py` (respx: request body, 768 floats, empty batch, wrong shape, errors naming the host, not configured, timeout); `contract/test_embedding_port.py` (fake and adapter); `contract/test_matching_tools.py::test_s4_ac_08_an_unavailable_embedding_host_keeps_its_class` (over MCP too) |
| AC-C-01 "through the tool gateway" | `contract/test_matching_tools.py` (both transports, both units of work); `unit/agents/test_transport_equivalence.py` (matching runs in every scenario, over both transports) |
| **AC-C-02** | `unit/agents/test_matching_graph.py::test_ac_c_02_the_matching_agent_has_read_tools_only` (toolbox, TOOL events by agent, `save_matches` by the SUPERVISOR, node agents); `contract/test_matching_tools.py::test_ac_c_02_a_failed_embedding_writes_nothing` and `::test_save_matches_refuses_what_does_not_exist_and_writes_nothing`; `unit/agents/test_receipt_graph_tools.py::test_s5_ac_05_*[product_lookup-…, product_similarity_search-…, save_matches-…]` |
| **AC-C-03** | `unit/agents/test_matching_graph.py::test_ac_c_03_new_then_alias_then_memory_after_a_correction` (A new, B alias with zero embedding and matching calls, `correct`, B reprocessed: line 0 `memory`, the trace's `save_matches` lines and summary); `integration/test_matching_sqlite.py`; `e2e/test_cli_items.py::test_ac_c_03_the_second_receipt_matched_by_alias` (terminal row `alias 3`) and `::test_s4_ac_06_correct_with_a_new_product_then_memory` |
| **AC-C-04** = S3-AC-08 | `unit/agents/test_resume.py::test_ac_c_04_*` (pause after `extract_text`, a second process resumes with zero LLM calls, `resumed_from="extract_text"`), `::test_a_breakpoint_after_read_skips_the_ocr_on_resume`, `::test_a_breakpoint_after_persist_reports_the_saved_status`; `integration/test_matching_sqlite.py::test_ac_c_04_*` (SqliteSaver); `e2e/test_cli_resume.py::test_ac_c_04_*` (trace `resumed_from`), `::test_an_unknown_breakpoint_is_a_usage_error` |
| **AC-C-05** = S3-AC-10 | `unit/agents/test_resume.py::test_ac_c_05_*` (PROCESSING/WAITING_FOR_GPU, checkpoint kept; resume asks only for the retry, which still carries the first attempt's feedback, `resumed_from="supervisor"`; a drop during matching resumes matching only, `resumed_from="persist"`), `::test_a_crash_after_save_matches_resumes_without_duplicates`, `::test_resuming_*`, `::test_a_*_has_nothing_to_resume`, `::test_a_receipt_waiting_for_review_is_resolved_not_resumed`; `e2e/test_cli_resume.py::test_ac_c_05_*` (exit 4 with the hint, `waiting_for_gpu`, exit 0 on resume), `::test_resume_with_the_gpu_still_down_is_exit_4`, and the refusals |
| `--reprocess` (plan 3.2, 4.1) | `unit/agents/test_resume.py::test_reprocess_*`, `::test_without_reprocess_the_same_photo_is_a_duplicate`; `e2e/test_cli_resume.py::test_reprocess_*` |
| S5-AC-04 for S4 | `unit/agents/test_transport_equivalence.py`; `contract/test_matching_tools.py` |
| Settings | `unit/test_config_agent_core_c.py`; the generic `.env.example` hygiene test picks up the new fields |
| Review note 1 (MCP timeout) | `unit/test_config_agent_core_c.py::test_review_note_1_*` |
| Review note 2 (PLAN 7) | Documentation only; checked in review. The wording test was removed (test-suite cleanup, `docs/tests/test-suite-cleanup.md`) |
| Review note 3 (one transport helper) | `unit/test_cli_agent_core.py::test_review_note_3_*`; `repo/test_agent_core_c_repo.py::test_review_note_3_*` |
| Review note 4 (gateway port, text-only result) | `unit/adapters/test_mcp_gateway.py::test_review_note_4_*` |
| E2 commands | The commands themselves: `e2e/test_cli_items.py` (`items correct --run`), `e2e/test_cli_resume.py` (`process --run ... --resume`). The plan and README wording tests were removed (test-suite cleanup, `docs/tests/test-suite-cleanup.md`) |

Out of slice C, and not tested here:

- Enrichment ("enriches it" in S4-AC-04), `save_enrichment`, category memory applied to products (S4-AC-05): slice D. Slice C records CATEGORY corrections so slice D can apply them.
- The `UNMATCHED_PRODUCT` review for a low-confidence confirmation (PLAN 6): see decision 3.
- The HTTP form of the correction (`PATCH /api/receipts/{id}/items/{item_id}`): Stage 6.
- A match-source column in the run's summary table and `summary.json` (plan 4.2): not required by an AC; cuttable with `runs compare`.

## Details the plans left open, pinned by the tests

### Domain

- `ItemLink(position: int, product_id: int, match_source: MatchSource)`, frozen with slots, in `domain/models.py`.
- `normalization.GENERIC_ALIASES` (a frozenset of normalized aliases, at least `produce` and `deposit`, each its own normalized form) and `is_generic_alias(alias) -> bool` (True also for an empty alias).

### Ports

- `ports/embeddings.py`: `EmbeddingPort.embed(texts: Sequence[str]) -> tuple[tuple[float, ...], ...]`, one 768-long vector per text in order; `()` for no texts; raises `LlmUnavailableError` (`ports/llm.py`).
- `ReceiptRepository.link_items(receipt_id, links: Sequence[ItemLink]) -> Receipt`: sets `product_id` and `match_source` at those positions; line ids and everything else unchanged, `updated_at` stamped; NotFoundError (and no change) for an unknown receipt or position.
- `PriceRepository.reassign(line_item_id, product_id) -> PriceObservation | None`.
- `replace_contents` drops the replaced lines' price observations (SQLite: the cascade).
- `ReceiptGraphRunner.start(..., interrupt_after: str | None = None)`; `GraphOutcome.resumed_from: str | None = None` (the last node completed before a run that continued a thread). A pause returns `paused_at=<node>`, no review, and the receipt's saved status (PROCESSING before `persist`). `resume(receipt_id)` raises ConflictError when the receipt has no thread or its thread is finished.

### Settings

- `matching: MatchingSettings` (`PAPER_TRAIL_MATCHING__*`), frozen: `auto_link_max_distance = 0.15`, `review_max_distance = 0.30`, both in 0..2, `review >= auto_link` (equal means no confirmation band).
- Review note 1: `Settings` refuses `MCP__TIMEOUT_S` unless it is greater than `OCR__TIMEOUT_S` and `OLLAMA__REQUEST_TIMEOUT_S`, naming `MCP__TIMEOUT_S`; the default satisfies it (the value is the implementer's; `.env.example` follows).

### Embedding adapter

- `adapters/ollama/embeddings.py`: `OllamaEmbeddings(settings: OllamaSettings)`; `POST <base_url>/api/embed` with `{"model": embed_model, "input": [...], "keep_alive": keep_alive}`, timeout `request_timeout_s`; a vector count or length that does not fit is `LlmOutputError`; connection errors, timeouts and non-2xx answers are `LlmUnavailableError` naming the host; no `base_url` is "not configured" and sends nothing.

### Tools

`ToolServices` gains `embeddings: EmbeddingPort`. Allowlists are PLAN 7's (matching: lookup and search; enrichment: lookup; supervisor: `save_matches`).

- `product_lookup {alias, store_key: str | null = null}` -> `{product: ProductOut | null, source: "memory" | "alias" | null}`. Normalizes `alias`. Memory first: the newest PRODUCT_LINK correction for exactly (alias, store_key), whose `new_value` is the product id; then the store's alias, then a global one. `ProductOut = {id, canonical_name, brand, size_value: str | null, size_unit, category, tags}`.
- `product_similarity_search {text, k: 1..10}` -> `{candidates: [{product: ProductOut, distance}]}`: embeds `text` as given in one call, nearest first, at most `k`. (PLAN 7 wrote `-> [CandidateOut]`; an MCP structured result must be an object.)
- `save_matches {receipt_id, decisions: [{position, source, product_id | null, new_product_name | null}]}` -> `{receipt_id, linked, new_products, prices, lines: [{position, product_id, source}]}`. Exactly one of `product_id` and `new_product_name`; `source` is `new` exactly when a new product is named (else `ToolInputError`). One unit of work: one new product per distinct line alias (canonical name as given), `link_items`, a MATCHED alias per linked line (a USER_CORRECTION alias is kept), the new products' vectors (their aliases embedded in one call, in line order; none without new products), a price observation per linked ITEM line when the purchase date is known; the receipt is COMPLETED/DONE afterwards. Unknown receipt, position or product: NotFoundError and nothing written; a failed embedding call writes nothing either. Idempotent: the same decisions again leave the same products, links, aliases and prices.
- `tools/wire.py` gains the kind `llm_unavailable` (`LlmUnavailableError`), so a GPU drop inside a tool stops the run with its checkpoint over MCP too.
- Trace summary of `save_matches`: the count per source first, in `MatchSource` order (`memory 1 · alias 2`), then new products and prices; `data["output"]["lines"]` is the per-line record.

### Graph and matching

- `persist -> match (MATCHING) -> commit_matches (SUPERVISOR) -> END`; `agents.graph.NODE_NAMES` is the tuple of node names. `commit_matches` calls `save_matches` once per run, with an empty list when nothing matched.
- `match`, per ITEM line in position order whose alias is not generic, with `store_key = normalize_store_name(store_name)`: `product_lookup` (hit: `memory` or `alias`); else `product_similarity_search(alias, k=3)`: nearest at most `auto_link_max_distance` -> `vector`; else, nearest at most `review_max_distance` -> one `matching/v1` confirmation among the candidates within that distance; `same` with an offered id and `confidence >= GRAPH__MIN_CONFIDENCE` -> `llm`; anything else, or no candidate -> `new`, named after the line's `description`. DEPOSIT, FEE and DISCOUNT lines and generic aliases: no tool call, no link.
- The confirmation request: text model, temperature 0, tag `matching/v1`; the final user message holds the line's raw text and each offered candidate's id and name inside `<receipt_data>`. An answer that never validates counts as `different` (not retried; the adapter already repaired once).

### Prompt

- `agents/schemas.py`: `MatchDecision {line_position: StrictInt >= 0, decision: "same" | "different", product_id: StrictInt | null, confidence: 0..1}`, `extra="forbid"`.
- `agents/prompts/matching/v1/{system.md, user.md, schema.json}`; `schema.json` equals the Pydantic schema; the system prompt states the data rule and both decisions; examples are optional and use `{"ocr_text", "output"}`.
- `run_config` records `prompt_versions = {"extraction": ..., "matching": ...}` and `models["embed"]`.

### Use cases

- `application/corrections.py`: `CorrectLineItem(uow_factory, clock)`, `__call__(receipt_id, position, *, product_id=None, new_product_name=None, category=None) -> LineItem`. One unit of work: the product (new: name masked, `category`), the line relinked as MEMORY, its price observation moved (never created), a PRODUCT_LINK correction (subject alias, old and new product ids, store, receipt, line), a USER_CORRECTION alias; with `category` also a CATEGORY correction (an existing product's category is left to slice D). No embedding or LLM call. DomainValidationError unless exactly one target; NotFoundError for an unknown receipt, position or product.
- `ProcessReceiptFile.__call__(source, *, reprocess=False, interrupt_after=None)`. Reprocessing a stored image dismisses the receipt's OPEN review, resets the thread, sets PROCESSING/READING and starts from the stored OCR text (from the image when none is stored); `duplicate=False`, same id.
- `ProcessReceiptFile.resume(receipt_id) -> ProcessOutcome`: as `__call__` for failures (`ProcessingStoppedError` for a GPU drop, FAILED outcome otherwise); NotFoundError for an unknown receipt; ConflictError naming `review resolve` when it has an OPEN review.
- A GPU drop while the receipt is PROCESSING sets its stage to WAITING_FOR_GPU; a receipt already saved keeps status and stage.

### CLI

- `process --interrupt-after NODE`: a name from `NODE_NAMES`, else exit 2 listing them, before anything runs. A pause prints `paused after <node>` and `paper-trail process --resume <id>`, exit 0.
- `process --resume ID [--run NAME]`: no PATH, `--text-file` or `--reprocess` (exit 2); exit 1 for an unknown receipt, nothing to resume or a pending review (naming `review resolve`); exit 4 when the LLM host is down; the trace records `resumed_from`.
- `process PATH --reprocess`; with `--text-file` it is exit 2.
- Exit 4 always prints `paper-trail process --resume <id>`.
- `items correct RECEIPT_ID POSITION (--product-id N | --new-product NAME) [--category C] [--run NAME]`: prints `receipt <id> line <position>: product <id> <name>`; exit 2 for neither or both targets or an unknown category; exit 1 for an unknown receipt, line or product, or an unmigrated main database.
- Review note 3: `interfaces/cli/options.with_transport(settings, transport) -> Settings` (in `process.py` until the test-suite cleanup moved it, so `main` can declare the option without importing the agent commands) is the one place `--transport` is applied (`with_flags` and the `tools` commands use it).

### MCP gateway (review note 4)

- `host:port` in messages uses the scheme's default port (80 for http, 443 for https).
- A successful result without `structuredContent` is its text content parsed as one JSON object; otherwise `ToolOutputError` naming the tool.

## Decisions for the owner or orchestrator to confirm

1. **Positions are 0-based in `items correct`**, as stored and as `receipts show` prints them. The arithmetic messages say "line 3" (1-based), so a person reading those could be off by one. The alternative is 1-based positions in the CLI only.
2. **`save_matches` is idempotent and leaves the receipt COMPLETED/DONE**, the same safeguard as `open_review` in slice B: a crash between its commit and the checkpoint re-runs it on resume.
3. **A low-confidence confirmation creates a new product** instead of opening an `UNMATCHED_PRODUCT` review (PLAN 6). A review after the receipt is saved would need a second interrupt point in the graph; the threshold reuses `GRAPH__MIN_CONFIDENCE`.
4. **Review note 1 is solved by validation, not derivation**: `MCP__TIMEOUT_S` must exceed the OCR and LLM request timeouts, with a default that does. The 30 s value in `.env.example` must change.
5. **`process --resume` and `items correct` take `--run`**, and the plan's E2 commands were changed to pass it (and to resume through `make pt`).
6. **`GraphOutcome.resumed_from`** carries the node a run continued after, so the CLI does not need a separate runner query. A review answer keeps the CLI's existing `resumed_from = "review"`.
7. **A GPU drop marks the receipt WAITING_FOR_GPU only while it is PROCESSING** (S3-AC-10 says "sets WAITING_FOR_GPU"); after the save, a drop during matching leaves it COMPLETED with unlinked lines until `--resume`.
