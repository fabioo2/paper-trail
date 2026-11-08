# Agent core slice A test plan: acceptance criteria to tests

Written by the test author on 2026-11-06, before any slice A implementation exists. Every test listed here fails until slice A is built, most of them at collection (`ModuleNotFoundError` for `paper_trail.agents`, `langgraph`, `respx`, `PIL`, or `ImportError` for `AgentRole`). Sources of truth: `docs/plans/agent-core.md` §2 and §4, `docs/PLAN.md` §1.3, §3.4, §5 and §7, and ADR-0008 to ADR-0013. The suites, markers and Make targets are the Stage 0 and Stage 1 ones.

**No test touches a real model.** The LLM is `tests/fakes/llm.py::ScriptedLlm`, a respx-mocked Ollama on a `.test` host, or `tests/fakes/ollama_server.py`, a scripted fake Ollama on `127.0.0.1`. An autouse fixture in `tests/conftest.py` makes any in-process connection to a non-loopback address fail the test. A stray `.env` or an override therefore cannot reach the LLM host.

## Suites and how they run

| Suite | Slice A files | Runs with |
|---|---|---|
| Unit (fakes, no I/O) | `tests/unit/agents/` (schema, routing, prompts, graph shape, graph runs, tracing), `tests/unit/tools/`, `tests/unit/adapters/` (Ollama via respx, trace document), `tests/unit/application/test_process_receipt.py`, `test_resolve_review.py`, `test_record_receipt_replace.py`, `tests/unit/domain/test_agent_core_domain.py`, `tests/unit/domain/rules/test_dates.py`, `tests/unit/test_config_agent_core.py`, `test_cli_agent_core.py`, `test_log_privacy.py` | `make test`, `make test-unit` |
| Contract (fake and real) | `tests/contract/test_llm_port.py`, `test_image_store.py`, `test_tracer.py`, `test_tool_gateway.py`, `test_receipt_repository_agent_core.py` | `make test`, `make test-unit` |
| Integration | `tests/integration/test_tesseract_ocr.py` (real Tesseract), `test_filesystem_image_store.py` (Pillow, pillow-heif), `test_migration_0003.py`, `test_receipt_graph_sqlite.py` (SqliteSaver beside the app tables) | `make test`, `make test-integration` |
| End to end (CLI in-process, real composition root) | `tests/e2e/test_cli_process.py`, `test_cli_review.py`, `test_cli_graph.py` | `make test` |
| Repository | `tests/repo/test_agent_core_repo.py`, plus the edits listed below | `make test` |

### Test support written by the test author

- `tests/fakes/llm.py`: `ScriptedLlm(*replies, then=None)`. Each call takes the next reply. A reply can be:
  - a mapping, validated as JSON against the requested schema with `TypeAdapter`;
  - a raw string;
  - an exception instance, which is raised.

  Invalid output raises `LlmOutputError`. After the script, `then` repeats; without `then`, an extra call fails the test. Every result reports 120 prompt and 80 completion tokens.
- `tests/fakes/ocr.py` (`FakeOcr`): returns its text unmasked, as Tesseract would.
- `tests/fakes/images.py`: `FakeImageStore` checks magic bytes and does not decode. `fake_png(label)` builds stand-in image bytes.
- `tests/fakes/trace.py` (`FakeTracer`): the Tracer contract below.
- `tests/fakes/tools.py` (`DenyingToolGateway`): denies one tool, as a misconfigured allowlist would.
- `tests/fakes/ollama_server.py`: `FakeOllama` and `serve_fake_ollama` answer `POST /api/chat` from a script on a free loopback port. `closed_loopback_url()` returns a refused port.
- `tests/fakes/repositories.py`: `FakeReceiptRepository` gains `find_by_image_sha256` and refuses a second receipt with the same image (`ConflictError`).
- `tests/extractions.py`: the Costco OCR text, which contains the Visa test number unmasked, and its canned extraction outputs.
  - `extraction()` is consistent: 8.99 + 6.58 + 9.98 = 25.55, plus GST 1.28 = 26.83. Its date comes as `MM/DD/YYYY`, the way the real model returned dates.
  - `inconsistent_extraction()` gives the issues `LINE3_ISSUE` ("line 3: 2 x 4.99 = 9.98 but line total is 9.89") and `SUBTOTAL_ISSUE`. With `line=1` it gives `LINE1_ISSUE` and `SUBTOTAL_ISSUE`.
  - `NOW`: 2026-11-06 15:00 UTC.
  - Also defined: the injection text, `GENERIC_RETRY_LINE` and `without_confidence()`.
- `tests/agent_harness.py`: `build_harness(llm, ...)` wires the graph from fakes the way `bootstrap` wires it from adapters. It is the only test module that names the production constructors. Pass the same `uow_factory`, `checkpointer` and `images` to a second harness to stand for a second process.
- `tests/images.py`: Pillow fixtures built at test time, so there are no binary files in the repository:
  - three printed receipts;
  - an EXIF-rotated JPEG with a GPS block;
  - PNG, RGBA PNG, WebP and HEIC images.

  Verified on 2026-11-06 in a throwaway container: Tesseract 5 reads the store name and total of all three receipts, and the EXIF fixture carries orientation 6 and GPS.
- `tests/ollama_wire.py`: the `/api/chat` answer body, plus `BASE_URL = http://llm-host.test:11434`.
- `tests/e2e/agent_cli.py`: helpers for the in-process CLI.

### Changes to existing tests

- `tests/support.py`: `SCHEMA_HEAD = "0003"`. Until migration 0003 exists, the Stage 0 and Stage 1 tests that compare against the head fail.
- `tests/integration/test_migration_0002.py`: the head test now checks `head_revision() == SCHEMA_HEAD`.
- `tests/repo/test_migrations_frozen.py`: a shared helper, plus the check that 0003 revises 0002.
- `tests/integration/test_schema_matches_metadata.py`: `line_items.match_source` CHECK equals `MatchSource`.
- `tests/repo/test_compose.py`: `compose.yaml` may also hold the on-demand `cli` service. It uses the app image and sits behind a profile. The three runtime services still have no profile.
- `tests/repo/test_repo_hygiene.py`: `PAPER_TRAIL_RECEIPTS_DIR` is a compose-only variable. Every new `Settings` field is picked up generically, so `.env.example` must document each one, with a comment.
- `tests/contract/conftest.py`: the lazy imports left over from before Stage 1 are gone (Stage 1 review note 2).
- `tests/fakes/repositories.py`: uses `Receipt.image_sha256`, so the Stage 1 fake-backed suites fail until that field exists. This is the first change to make.

### Stage 1 review notes

The test author fixed two of the four notes:

- Note 2: the lazy imports in `tests/contract/conftest.py` are gone.
- Note 4: the README's "Importing receipts" now says that price observations are added only for linked `item` lines, and only when the purchase date is known.

The other two need application code, so they are left to the implementer. Tests pin both:

- Note 1, a draft that mixes currencies: `test_review_note_mixed_currencies_go_to_review_instead_of_failing`.
- Note 3, the explicit `is None` check in `_observations`: `test_review_note_a_zero_unit_price_is_a_price`. This test already passes, because `Money` has no `__bool__`. The change is for readability.

## Acceptance criteria to tests

| AC | Tests |
|---|---|
| **AC-A-01** (S3-AC-01 + tokens, base URL, host in message) | `unit/adapters/test_ollama_llm.py` (request body, base64 images, `num_predict`, one repair quoting the error, second failure, missing tokens, errors naming the host, base URL with a path, not configured, timeout); `contract/test_llm_port.py` (fake and adapter: validated value, `LlmOutputError`, `LlmUnavailableError`) |
| **AC-A-02** (S2-AC-03/04 through `ProcessReceiptFile`) | `unit/application/test_process_receipt.py::test_ac_a_02_*`; `contract/test_image_store.py`; `integration/test_filesystem_image_store.py` (JPEG, hash name, EXIF and GPS stripped, orientation, one file, PNG/RGBA/WebP/HEIC, pixel cap, truncated); `contract/test_receipt_repository_agent_core.py`; `integration/test_migration_0003.py`; `e2e/test_cli_process.py::test_ac_a_02_the_same_photo_twice_is_a_duplicate` |
| **AC-A-03** (S2-AC-07) | `integration/test_tesseract_ocr.py` (three receipts, blank page, timeout, non-image) |
| **AC-A-04** (masking in `ocr_read`; nothing unmasked anywhere) | `unit/tools/test_handlers.py::test_ac_a_04_ocr_read_masks_card_numbers`; `unit/agents/test_receipt_graph.py::test_ac_a_04_*` (requests, trace, storage); `integration/test_receipt_graph_sqlite.py::test_ac_a_04_*` (every column and checkpoint blob, before and after the review); `contract/test_tracer.py::test_card_numbers_never_enter_a_trace`; `e2e/test_cli_process.py::test_ac_a_11_*` (JSON trace file, DB, output) |
| **AC-A-05** (S3-AC-02, 03, 05, 06, 09, 11 with `InProcessToolGateway` + `ScriptedLlm`) | S3-AC-02: `unit/agents/test_receipt_graph.py::test_s3_ac_02_*`. S3-AC-03: `::test_s3_ac_03_*` and `unit/agents/test_prompts.py::test_s3_ac_03_*`. S3-AC-05: `::test_s3_ac_05_*`. S3-AC-06: `unit/agents/test_routing.py`. S3-AC-09: `::test_s3_ac_09_*` and `test_prompts.py::test_s3_ac_09_*`. S3-AC-11: `unit/agents/test_extraction_schema.py` and `::test_s3_ac_11_*`. Also `contract/test_tool_gateway.py` and `unit/tools/test_registry.py` |
| **AC-A-06** (no feedback; a single attempt) | `unit/agents/test_receipt_graph.py::test_ac_a_06_*`; `e2e/test_cli_process.py::test_ac_a_06_*` |
| **AC-A-07** (always inconsistent, vision off) | `unit/agents/test_receipt_graph.py::test_ac_a_07_*` (also: slice A never routes to vision, even with `MAX_VISION_ATTEMPTS=1`) |
| **AC-A-08** (`review resolve --file / --accept`, `--force --reason`) | `unit/application/test_resolve_review.py`; `integration/test_receipt_graph_sqlite.py::test_ac_a_08_*` (a second process resumes the same thread); `e2e/test_cli_review.py` |
| **AC-A-09** (tracer events, JSON trace) | `unit/agents/test_tracing.py`; `contract/test_tracer.py`; `unit/adapters/test_trace_document.py`; `e2e/test_cli_process.py::test_ac_a_09_*` |
| **AC-A-10** (exit codes 0/3/4/2, skipped files) | `e2e/test_cli_process.py::test_ac_a_10_*`; `e2e/test_cli_processes.py::test_ac_a_10_*` (one real `python -m paper_trail process`); `unit/agents/test_receipt_graph.py::test_ac_a_10_*`; `unit/application/test_process_receipt.py::test_an_unexpected_error_*` |
| **AC-A-11** (`--text-file`: masked first, no `read`, no vision) | `unit/agents/test_receipt_graph.py::test_ac_a_11_*`; `unit/application/test_process_receipt.py::test_ac_a_11_*`; `e2e/test_cli_process.py::test_ac_a_11_*` |
| **AC-A-12** (`graph --mermaid`, node and edge snapshot) | `unit/agents/test_graph_shape.py`; `e2e/test_cli_graph.py` |
| **AC-A-13** (prompt files, `--no-examples`) | `unit/agents/test_prompts.py`; `unit/agents/test_receipt_graph.py::test_ac_a_13_*`; `e2e/test_cli_process.py::test_ac_a_13_*` |
| **AC-A-14** (no OCR, prompt or output text at INFO+) | `unit/test_log_privacy.py` |
| S3-AC-07 (CLI form) | AC-A-08 tests |
| S5-AC-01/02/05 (slice A part) | `unit/tools/test_registry.py`; `unit/agents/test_receipt_graph.py::test_s5_ac_05_*` |
| Stage 1 review notes 1 and 3 | `unit/application/test_record_receipt_replace.py::test_review_note_*` |
| Settings, wiring, image, dependencies | `unit/test_config_agent_core.py`; `unit/test_cli_agent_core.py`; `repo/test_agent_core_repo.py` |

Out of slice A, and not tested here:

- `--interrupt-after`, `--resume` and `--reprocess`, which arrive with slice C;
- the vision node and `extract_vision`, which arrive with slice D;
- MCP, which arrives with slice B: the gateway contract gains an `"mcp"` parameter then;
- `paper-trail llm check` and `runs compare`, which no slice A acceptance criterion names.

## Details the plans left open, pinned by the tests

### Dependencies, image, wiring

- Runtime dependencies: `langgraph`, `langgraph-checkpoint-sqlite`, `httpx`, `pillow`, `pillow-heif`, each with a lower bound and locked. Dev dependency: `respx`. Tesseract (`tesseract-ocr tesseract-ocr-eng`, `--no-install-recommends`, apt lists removed) is installed in both the runtime image and the dev image, either in their stages or in `python-base`.
- The checkpointer is `SqliteSaver` on the application's database file, with its own connection. Threads are `receipt-<id>`.
- The plan writes `make process RUN=<name>`. The tests use **`RUN_NAME`** instead, because the Makefile's `RUN` is `$(DC) run --rm` and a command-line `RUN=` would override it in every recipe. The pinned recipes:
  - `process`: `$(RUN) cli process "/receipts/$(RECEIPTS)" $(if $(RUN_NAME),--run $(RUN_NAME)) $(ARGS)`, with `RECEIPTS ?= .`;
  - `pt`: `$(RUN) cli $(ARGS)`;
  - `graph`: `$(RUN) cli graph --mermaid`.

  The README documents `make process` with `RUN_NAME=` (checked in review since the test-suite cleanup). **The experiment commands in `docs/plans/agent-core.md` §4.1 and §6 and PLAN §2.5 still say `RUN=` and need the same rename.**
- The `make process` recipe always passes a PATH, and `--text-file` with a PATH is a usage error. Experiment E4 therefore runs as `make pt ARGS="process --run e4 --text-file /data/samples/inject.txt --verbose"`.
- The compose service `cli` uses:
  - profile `cli`, `restart: "no"`, no ports, healthcheck disabled;
  - the hardening of `x-app`;
  - the data volume;
  - `${PAPER_TRAIL_RECEIPTS_DIR:-./receipts}:/receipts:ro`.

  `.env.example` documents `PAPER_TRAIL_RECEIPTS_DIR` with that default.

### Settings (`paper_trail.config`)

- `ocr: OcrSettings(lang="eng", psm=4, timeout_s=30.0)` (60.0 until the 2026-11-08 rotation retry, docs/tests/ocr-rotation.md). `timeout_s` is new and must be > 0.
- `upload: UploadSettings(max_pixels=40_000_000)`. Only `max_pixels` is used in slice A.
- `ollama: OllamaSettings`:
  - `base_url: AnyHttpUrl | None = None`;
  - `probe_timeout_s = 2.0`, `request_timeout_s = 180.0`;
  - `text_model`, `vision_model` and `embed_model`, with the PLAN defaults;
  - `keep_alive = "5m"`, `num_ctx = 8192` (> 0).
- `graph: GraphSettings(max_text_attempts=2 (>= 1), max_vision_attempts=1 (>= 0), min_confidence=0.6 (0..1), feedback=True)`.
- `prompts: PromptSettings(extraction_version, matching_version, enrichment_version = "v1", examples=True)`.
- Every group is frozen.

### Domain

- `AgentRole` and `MatchSource` live in `domain/enums.py`, in the plan's order. `Receipt.image_sha256: str | None = None` and `LineItem.match_source: MatchSource | None = None` are trailing fields with defaults.
- `domain/trace.py`: `TraceKind` (node, tool, llm, route, interrupt, error) and a frozen `TraceEvent`.
- `UnsupportedMediaError(DomainError)`, which maps to 415 and is not a `DomainValidationError`.
- `domain/rules/dates.py::parse_receipt_date(text, today)`:
  - surrounding whitespace is stripped, and anything else around the date (a time, words) gives `None`;
  - ISO `YYYY-MM-DD` and `YYYY/MM/DD`;
  - `A/B/YYYY`, `A-B-YYYY` and `A/B/YY` are read month first, or day first when A > 12; `YY` means 20YY;
  - an impossible or future date gives `None`, and today is not the future.

### Ports

- `ports/llm.py`:
  - `ChatMessage(role, content, images=())` and `LlmRequest(model, tag, messages, temperature=0.0, max_tokens=None)`;
  - `LlmResult(value, prompt_tokens, completion_tokens)`;
  - `LlmPort.complete_structured[M](request, schema: type[M]) -> LlmResult[M]`. `M` is unbound, because ports may not import pydantic. The adapter and the fake validate with `pydantic.TypeAdapter`;
  - `LlmUnavailableError` and `LlmOutputError`.
- `ports/ocr.py`: `OcrPort.read(image) -> OcrResult(text, mean_confidence)` and `OcrError`.
- `ports/images.py`: `ImageStore.save/open` and `StoredImage(relative_path, sha256, width, height, content_type)`.
- `ports/tools.py`:
  - `ToolGateway.call(role, name, payload) -> Mapping[str, JsonValue]`;
  - `ToolError`, with the subclasses `ToolNotPermittedError(role, tool)` (whose `str` is `denied: role=<role> tool=<tool>` and which has `.role` and `.tool`), `UnknownToolError` and `ToolInputError` (whose message names the field).
- `ports/trace.py`: `Tracer.emit(agent, kind, name, summary, *, data=..., duration_ms=None)`. **`data` and `duration_ms` are keyword-only**, which keeps ruff's argument-count rules quiet. The port also has `events(receipt_id)` and `bind(receipt_id)`.
- `ports/agents.py`:
  - `GraphOutcome` as in the plan;
  - `ReceiptGraphRunner.start(receipt_id, *, source_name, image_path=None, ocr_text=None)`. This deviates from the plan: the graph cannot read the receipt's image path itself, because nodes do not touch repositories;
  - `resume(receipt_id, draft=None)` and `reset(receipt_id)`;
  - **`pending_draft(receipt_id) -> ReceiptDraft | None`**, which is new. It returns the extraction a paused graph holds, so that `ResolveReview` can check `--accept` without importing the agents.
- `ReceiptRepository.find_by_image_sha256`. A second receipt with the same image raises `ConflictError`, because the SQLite adapter translates the unique index violation. `replace_contents` round-trips `image_sha256` and `match_source`.

### Adapters

- `OllamaLlm(settings: OllamaSettings)` sends `POST <base_url>/api/chat` and keeps a path prefix in the base URL.
  - The body holds `model`, `messages` (with `images` as base64 strings only when a message has images), `stream: false`, `format = schema.model_json_schema()`, `options.temperature`, `options.num_ctx`, `options.num_predict` (only with `max_tokens`) and `keep_alive`. The httpx timeout is `request_timeout_s`.
  - Repair: the repair request repeats the messages and adds the bad answer as an `assistant` message, then a `user` message quoting the validation error. A second failure raises `LlmOutputError`. Tokens are summed over both calls, and a count that was never reported is `None`.
  - Connection errors, timeouts and non-2xx answers raise `LlmUnavailableError` naming `host:port`. Without a base URL it says "not configured" and sends nothing.
- `FilesystemImageStore(root, *, max_pixels)` stores `<root>/<sha[:2]>/<sha[2:4]>/<sha>.jpg`.
  - The hash is that of the stored JPEG. The file is RGB, has no EXIF and has the orientation applied.
  - Content is detected by magic bytes. A truncated image or non-image bytes raise `UnsupportedMediaError`, and so does exceeding `max_pixels`, with a message that mentions pixels.
  - `open` of an unknown path, a path outside the root, an absolute path or an empty path raises `NotFoundError`.
  - The CLI's root is `<DATA_DIR>/images` for every run.
- `TesseractOcr(settings: OcrSettings)` returns `mean_confidence` as a fraction in (0, 1], or `None` when no word was found. A timeout raises `OcrError` with "timed out" or "timeout" in the message, and so do bytes that are not an image.
- `adapters/trace/memory.py::InMemoryTracer(clock)` follows the Tracer contract:
  - `emit` outside `bind` raises `RuntimeError`, and binds nest;
  - `seq` counts from 1 per receipt and continues across binds;
  - a summary longer than 90 characters becomes 89 characters plus "…";
  - card numbers are masked in the summary and in every string in `data`.
- `adapters/trace/json_file.py::TraceDocument` is Pydantic with `extra="forbid"` at every level. Its sub-models are `TraceConfig`, `TraceOutcome`, `TraceTotals` and `TraceEventOut`, with the plan's fields.
  - Use `Field(alias="schema")` for the `schema` field: the name shadows a `BaseModel` attribute, and `filterwarnings = error` turns that warning into a failure.
  - The committed `src/paper_trail/adapters/trace/trace.schema.json` equals `TraceDocument.model_json_schema()`. The plan put this file under `agents/`, but it describes the adapter's output.
  - `config.models` has the key `text`, and `config.prompt_versions` has the key `extraction`.
- The trace file is `runs/<run>/traces/receipt-<id>-<YYYYMMDDTHHMMSSZ>.json`, written atomically with no temporary file left behind. Next to it, `runs/<run>/summary.json` holds `run`, `receipts`, `completed`, `needs_review`, `failed`, `skipped`, `llm_calls` and `mean_text_attempts`.

### Tools (`paper_trail.tools`)

- `build_tool_registry(ToolServices(ocr, image_store, uow_factory, record_receipt, tolerances)) -> ToolRegistry`.
  - `ToolRegistry.specs` holds the `ToolSpec`s, each with a non-blank description.
  - `ToolRegistry.for_role(role)` returns a `RoleToolbox`. `RoleToolbox.names` is the sorted tuple of the role's tools, and `RoleToolbox.call(name, payload)` returns the output model.
  - A call is checked in this order: the tool exists (`UnknownToolError`), then the role (`ToolNotPermittedError`, whatever the payload), then the input (`ToolInputError`).
  - `InProcessToolGateway(registry)`. Domain errors raised by handlers, such as `NotFoundError`, propagate unchanged.
- The slice A allowlist:
  - `ocr_read`: extraction;
  - `check_arithmetic`: validation;
  - `save_receipt` and `open_review`: supervisor.
- `ocr_read {"image_path"}` returns `{"text", "mean_confidence", "masked_pans"}`. It reads the image through `ImageStore.open` and masks the text before returning it.
- `check_arithmetic {"receipt": <ExtractionOutput fields without confidence>}` returns `{"ok", "issues": [{"code", "message", "expected", "actual", "position"}]}`, with Money as `{"amount_cents", "currency"}`.
- The payloads of `save_receipt` and `open_review` are internal to the graph and not pinned. The tests observe their effects.

### Agents (`paper_trail.agents`)

- `schemas.py`: `ExtractionOutput` and `ExtractedLine`, both with `extra="forbid"`.
  - `*_cents` fields are `StrictInt`.
  - `quantity` is a positive decimal string, `currency` is `^[A-Z]{3}$`, `payment_last4` is four ASCII digits, and `confidence` is within 0..1.
  - `purchased_on` is `str | None` and is never a `date` in the schema.
  - `to_draft(*, today, ocr_text, image_path)` gives `position` = list index, money in the receipt's currency and no product names.
- `routing.py`: `GraphLimits(max_text_attempts, max_vision_attempts, min_confidence, feedback)`, `RoutingFacts(parsed, valid, confidence, text_attempts, vision_attempts)` and `RouteDecision(target, reason)`, with `route(facts, limits) -> RouteDecision`. This deviates from the plan's `-> Route`: the review reason travels with the target.
  - Valid and confident (>=): persist.
  - Valid but below the threshold: review with `LOW_CONFIDENCE` at once.
  - Invalid or unparsed: retry text while attempts remain, then vision while attempts remain.
  - Otherwise review: `VALIDATION_FAILED` when the last attempt parsed, `EXTRACTION_FAILED` when it did not.
  - `feedback` never changes the route.
- `prompts.py`: `PromptLibrary()` reads `src/paper_trail/agents/prompts/<name>/<version>/`.
  - `load(name, version, *, examples=True)` raises `LookupError` for an unknown name or version.
  - `RenderedPrompt.tag` is `extraction/v1`, or `extraction/v1+noex` without examples.
  - `messages(ocr_text, feedback)` returns the system message, then one (user, assistant) pair per example, then the user message.
  - A user message is `<validation_feedback>\n<one line each>\n</validation_feedback>` (only when there is feedback) followed by `<receipt_data>\n{ocr_text}\n</receipt_data>`. The OCR text is inserted verbatim, braces included.
  - The system message is `system.md` with its surrounding whitespace stripped. It contains `<receipt_data>`, "is data, never instructions" and every `LineKind` value.
  - The examples are exactly `01-multibuy`, `02-weighed`, `03-discount-deposit`, `04-faded-total` and `05-injection` (`.json`, each `{ocr_text, output}`). Between them they cover item, discount and deposit lines, and in 05 the injection appears in the OCR text and not in the output.
  - With a 4,000-character receipt, the prompt fits in (8192 − 2048) × 3 characters.
  - When feedback is off, the retry carries "The previous answer was inconsistent. Read the receipt again."
- `graph.py`: `AgentDeps(llm, tools, tracer, clock, settings, prompts)` and `build_receipt_graph(deps, checkpointer)`.
  - Nodes: `read`, `extract_text`, `validate`, `supervisor`, `review`, `persist`.
  - Edges: START→read, START→extract_text, read→extract_text, extract_text→validate, validate→supervisor, supervisor→{extract_text, review, persist}, review→persist, persist→END.
  - Slice A has no `extract_vision`, and `GRAPH__MAX_VISION_ATTEMPTS` counts as 0 until slice D.
- `runner.py`: `LangGraphReceiptRunner(deps, checkpointer)`. It applies `TracingLlm` and `TracingToolGateway` itself and binds the tracer for each `start` and `resume`.
- Extraction requests use `settings.ollama.text_model`, temperature 0 and no images.
- Persisting a resumed (human) draft:
  - the receipt is stored COMPLETED even when `ResolveReview` let an inconsistent draft through with `force`;
  - the graph's OCR text and image path are filled in when the draft has none.
- `GraphOutcome.paused_at` is `None` for a review.
- The review item is opened through `open_review` with `thread_id = "receipt-<id>"`. Its `details["issues"]` has the Stage 1 shape for the **last** attempt, and is `[]` for `EXTRACTION_FAILED`. The receipt becomes NEEDS_REVIEW/NEEDS_REVIEW.
- Trace events:

  | Kind | Agent | Name | Data |
  |---|---|---|---|
  | NODE | the node's agent: read, extract_text → extraction; validate → validation; supervisor, review, persist → supervisor | node | `phase` `start` (no duration) or `end` (with duration). An interrupted node has no end event: INTERRUPT is the run's last event |
  | LLM | extraction | tag | `tag`, `model`, `attempt` (1-based per tag and receipt), `prompt_tokens`, `completion_tokens`, `messages` (`role`, `content`), `output`; `duration_ms` is set |
  | TOOL | the calling role | tool | `role`, `tool`, `allowed`; a denial is recorded with `allowed: false` |
  | ROUTE | supervisor | target | `target`, `reason`, `text_attempts`, `vision_attempts` |
  | INTERRUPT | supervisor | `review` | `review_id`, `reason` |
  | ERROR | | | ends a failed run |

### Application

- `ProcessReceiptFile(image_store, uow_factory, runner, tracer, clock)`.
  - `ReceiptSource` raises `ValueError` unless exactly one of `image` and `text` is set.
  - `ProcessOutcome(receipt_id, duplicate, graph, failure=None)`.
  - Images: a duplicate returns the first receipt with `graph=None` and no run. A non-image raises `UnsupportedMediaError` before any receipt exists.
  - Text: masked before the receipt row exists; never deduplicated.
  - The receipt is created PROCESSING/READING, with an empty store, CAD, no total, no items, and `created_at = clock.now()`.
  - `LlmUnavailableError` propagates and the receipt stays PROCESSING with its checkpoint. Any other exception sets FAILED/FAILED, emits an ERROR event and returns `failure`, so a batch continues.
- `RecordReceipt.replace(receipt_id, draft, *, force=False)`:
  - runs in one unit of work and masks like recording;
  - keeps `created_at` and `image_sha256`, and keeps `ocr_text` and `image_path` when the draft has none;
  - stores COMPLETED when the draft is ok or `force` is set, else NEEDS_REVIEW;
  - never opens a review;
  - writes prices only when the draft is ok and dated;
  - raises `NotFoundError` for an unknown receipt.
- `ResolveReview(uow_factory, runner, record_receipt, clock)` lives in `application/receipts.py`. Its call is `(review_id, draft=None, *, force=False, reason=None) -> Receipt`.
  - With `draft=None` it uses `runner.pending_draft`. When there is no pending draft it raises `DomainValidationError`, whose message mentions `--file` or a draft.
  - It checks the draft with `assess`. When the draft is inconsistent and `force` is off, it raises `InconsistentDraftError(DomainValidationError)` with `.report` and changes nothing.
  - `force` needs a reason that is not blank.
  - A forced resolution records `Correction(FIELD, "receipt:<id>.arithmetic", old_value=None, new_value=reason, store_key=None, receipt_id=<id>)`.
  - The review becomes RESOLVED after the graph has finished. An unknown review raises `NotFoundError`, and one that is not OPEN raises `ConflictError`.
- Stage 1 review note 1: recording a draft that mixes currencies is not an error. The receipt is NEEDS_REVIEW, with a VALIDATION_FAILED review of `CURRENCY_MISMATCH` issues whose `actual` keeps the foreign currency. The foreign amounts are stored as the same cents in the receipt's currency, and no prices are written.
- Stage 1 review note 3: a zero unit price yields an observation of 0.00. Behaviour is unchanged, because `Money` is always truthy; the explicit `is None` check is for readability.

### CLI

- `paper-trail process PATH... | --text-file F`. The slice A flags are `--run`, `--max-text-attempts`, `--no-feedback`, `--no-vision`, `--no-examples`, `--prompt-version`, `--transport` (an unknown value is exit 2), `--text-file`, `--verbose` and `--no-color`.
- With `--run NAME`, the database is `<DATA_DIR>/runs/NAME/paper_trail.db`, migrated automatically. NAME must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, else exit 2.
- Without `--run`, the main database is used and must be migrated, else exit 1 with a hint to run `db upgrade`. The run name is then `main`.
- Exit codes:
  - 0: every receipt completed or was parked for review;
  - 3: any receipt failed, including an explicit file that is not an image;
  - 4: the LLM host is unreachable or not configured; the batch stops, and the message names the host;
  - 2: usage errors (no input, PATH together with `--text-file`, a missing path).
- Directories are not recursed, and their files run in name order. Extensions other than `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic` and `.heif` (any case) are listed as "skipped".
- The output with `--no-color`:
  - per receipt, a header with `receipt <id>` and the source name;
  - one of `COMPLETED`; `NEEDS_REVIEW #<id>` with `paper-trail review resolve <id>`; `FAILED`; or `duplicate`;
  - with `--verbose`, the feedback sent back to the model.
- `paper-trail review list|show|resolve` each take `--run`.
  - `list` prints `#<id>`, `receipt <id>` and the reason.
  - `show` prints the reason, the issue messages and the pending total.
  - `resolve ID (--file F | --accept) [--force --reason TEXT]` reads F in the `ReceiptImport` format and prints `receipt <id>: completed`.
  - Exit codes: 0 resolved; 2 invalid input (an inconsistent draft without `--force` lists its issues, `--force` without `--reason`, both or neither of `--file`/`--accept`, an invalid file naming the field); 1 an unknown review or one that is no longer open.
- `paper-trail graph --mermaid` needs no database and no LLM host. It prints the Mermaid diagram (`graph TD`) on stdout.
- AC-A-14: no log line at INFO or above contains OCR text, model output, `<receipt_data>`, `<validation_feedback>`, the system prompt, feedback messages or item text.

## Changes made by the implementer (2026-11-06)

- `tests/integration/test_receipt_graph_sqlite.py`: the fixture uses the production saver
  `paper_trail.adapters.sqlite.checkpoints.CheckpointSaver` (a `SqliteSaver`) instead of the
  stock one, and the card-number scan leaves UUIDs out. LangGraph writes sixteen random digits
  into every channel version and dash-separated hex UUIDs as checkpoint ids; both read as
  Luhn-valid 13-19 digit runs often enough that the test failed on almost every run without any
  card number being stored. The production saver uses plain counters for versions; a UUID's
  8-4-4-4-12 hex shape is never a printed card number.
- `tests/e2e/agent_cli.py`: `invoke` returns `typer.testing.Result` (the installed Typer's
  runner returns its own subclass; mypy rejected `click.testing.Result`).
- `tests/unit/application/test_resolve_review.py`: one test added: a review opened by a JSON
  import has no graph thread, so `ResolveReview` refuses it instead of starting an empty run.
