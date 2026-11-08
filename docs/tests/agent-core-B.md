# Agent core slice B test plan: acceptance criteria to tests

Written by the test author on 2026-11-06, before any slice B implementation exists. Sources of truth: `docs/plans/agent-core.md` §3.1, §4 and §6 (E3, E4), `docs/PLAN.md` §1.3, §1.6, §2.4, §2.5 and §7, ADR-0008, ADR-0011 and ADR-0013, and the slice A review notes. Suites, markers, fakes and Make targets are the Stage 0, Stage 1 and slice A ones (`docs/tests/agent-core-A.md`).

**No test touches a real model or a real MCP host.** The LLM is `ScriptedLlm` or the scripted fake Ollama on 127.0.0.1. The MCP server is the production app served on a free 127.0.0.1 port inside the test process, or `paper-trail mcp` started as a child process bound to 127.0.0.1. The autouse loopback guard in `tests/conftest.py` still fails any other connection.

## How the tests fail before the implementation

17 modules fail at collection, because they import names that do not exist yet:

- `paper_trail.adapters.mcp` and `paper_trail.interfaces.mcp.server`;
- `McpSettings` and `ToolsSettings` in `paper_trail.config`;
- `ToolDescription` and `ToolOutputError` in `paper_trail.ports.tools` (`ToolUnavailableError` too);
- `neutralize_delimiters` in `paper_trail.agents.prompts`.

Every suite that builds the graph through `tests/agent_harness.py` is among them, because the harness can now put an MCP gateway in front of the registry. The other new tests collect and fail on their assertions:

- `find_open` on the SQLite review queue;
- the `tools` and `mcp` commands;
- `--transport mcp`;
- the compose service, the Make targets and the `mcp` dependency;
- the new import-linter contract;
- the `+noex` single-source check;
- `config.max_vision_attempts` in the trace.

Two tests already pass because the test author has done what they check:

- the plan's status is now owner-approved (review note 5);
- the plan's E4 path now matches the README (review note 5).

One test already passes because the behaviour is unchanged: the header tag equals the LLM events' tag (review note 3 is a refactor).

The implementer's first step is the missing names. With them, the whole suite collects again.

## Suites and how they run

| Suite | Slice B files | Runs with |
|---|---|---|
| Unit (fakes) | `tests/unit/tools/test_registry.py` (rewritten), `tests/unit/agents/test_prompt_delimiters.py`, `tests/unit/agents/test_receipt_graph_tools.py`, `tests/unit/adapters/test_mcp_gateway.py`, `tests/unit/test_config_agent_core_b.py`, `tests/unit/test_run_config.py`, `tests/unit/test_cli_agent_core.py` (added tests); `test_receipt_graph.py` and `tests/unit/application/test_resolve_review.py` ran once per transport until the test-suite cleanup; they now run in-process, and `tests/unit/agents/test_transport_equivalence.py` compares representative scenarios over both transports | `make test`, `make test-unit` |
| Contract | `tests/contract/test_tool_gateway.py` (in-process and MCP, each over the fake and the SQLite unit of work), `tests/contract/test_review_queue.py` (`find_open`) | `make test`, `make test-unit` |
| Integration | `tests/integration/test_receipt_graph_sqlite.py` (in-process tools since the test-suite cleanup) | `make test`, `make test-integration` |
| End to end | `tests/e2e/test_mcp_server.py` (in-test server plus a real `paper-trail mcp` process), `tests/e2e/test_cli_tools.py`, `tests/e2e/test_cli_process_mcp.py` | `make test` |
| Repository | `tests/repo/test_agent_core_b_repo.py`; additions to `test_import_contracts.py` and `test_compose.py` | `make test` |

Since the test-suite cleanup the graph suites run in-process only; S5-AC-04 is covered by `tests/unit/agents/test_transport_equivalence.py`, which runs representative scenarios over both gateways and requires equal results (the MCP side opens loopback sockets; it stays unmarked, so `make test-unit` runs it).

### Test support written by the test author

- **`tests/mcp_server.py`**
  - `serve_mcp(registry)` serves `create_mcp_app(registry)` with uvicorn on a free loopback port, in a background thread, and yields the base URL. Uvicorn runs with `log_config=None` and `lifespan="on"`.
  - `role_url(base, role)` returns `<base>/mcp/<role>/`.
  - `list_tools`, `call_tool` and `result_text` use the MCP SDK's own `mcp.Client`, an independent client.
- **`tests/agent_harness.py`**
  - `TOOL_TRANSPORTS = ("inprocess", "mcp")`.
  - `mcp_gateway(url)` returns `McpToolGateway(McpSettings)`.
  - `tool_transport(name)` is a context manager. Inside it, every harness built calls its tools through that transport: over MCP, each harness gets its own in-test server over its own registry.
  - Until the test-suite cleanup a `transport` fixture ran every graph test once per transport. It was replaced by `tests/unit/agents/test_transport_equivalence.py`: clean run, validation retry, review routing, denied tool, resume and review resolution run over both transports, and the outcomes, trace events, stored receipt, reviews, model calls and `tool.denied` logs must be equal.
- **`tests/fakes/tools.py`**
  - The decorators delegate `describe` to the gateway they wrap.
  - `MisroutedToolGateway(inner, tool, as_role)` re-addresses one tool's calls to another role. The denial then comes from the real registry, or the real server, with the production `tool.denied` log line.
  - `CrashAfterToolGateway(inner, tool)` lets the first call of a tool commit and then raises `SimulatedCrashError`.
- **`tests/fakes/queues.py`**: `FakeReviewQueue.find_open(receipt_id)`.
- **`tests/extractions.py`**
  - `E4_INJECTION` and `E4_SAMPLE_OCR`: the owner's E4 sample.
  - `E4_BREAKOUT` and `E4_BREAKOUT_OCR`: the same sample with a fake `</receipt_data>` … `<receipt_data>` around the injected line.

### Changes to existing tests

- `tests/contract/test_tool_gateway.py` is parametrized over `inprocess` and `mcp`, and over the `fake` and `sqlite` unit of work. It also gains tests for `describe`, domain errors keeping their class, equal results on both transports, and review note 4.
- `tests/unit/tools/test_registry.py`:
  - the slice A allowlist is replaced by PLAN 7's full table;
  - the matrix is generated from `AgentRole` × the registered tools;
  - every allowed pair is called with a valid sample payload;
  - output validation and registry construction checks are added.
- The following now run per transport, through `pytestmark`:
  - `tests/unit/agents/test_receipt_graph.py`;
  - `tests/integration/test_receipt_graph_sqlite.py`;
  - `tests/unit/application/test_resolve_review.py`.
- `tests/repo/test_compose.py`:
  - `ON_DEMAND_SERVICES = {"cli", "mcp"}`;
  - the default host ports are `{8420, 8421, 5173, 11434}`.
- `tests/repo/test_import_contracts.py` plants four imports for S5-AC-06:
  - `paper_trail.ports.repositories`;
  - `paper_trail.ports.unit_of_work`;
  - `paper_trail.ports.queues`;
  - `paper_trail.ports.search`.

  It also plants three adapter or interface imports, which existing contracts already catch.
- `docs/plans/agent-core.md` (review note 5, done by the test author):
  - the status is now "owner-approved (2026-11-06)";
  - E4 reads `/data/samples/inject.txt`, the path the README and `docs/tests/agent-core-A.md` use;
  - E4 also mentions the optional `</receipt_data>` breakout line.

## Acceptance criteria to tests

| AC | Tests |
|---|---|
| **AC-B-01** = S5-AC-01 | `unit/tools/test_registry.py::test_s5_ac_01_*`: every registered tool is in PLAN 7 with exactly its roles; only the supervisor writes; every allowed (role, tool) pair succeeds with a valid payload; every other pair is denied before the input is read, with the exact message, `.role` and `.tool`; plus `test_each_role_sees_exactly_its_tools` |
| AC-B-01 = S5-AC-02 | `unit/tools/test_registry.py::test_s5_ac_02_*`: invalid input names the field; unexpected fields are refused; output that does not validate, or is the wrong model, is a `ToolOutputError`. Also `contract/test_tool_gateway.py::test_invalid_input_is_reported_with_the_field` and `e2e/test_mcp_server.py::test_invalid_input_is_an_error_result_naming_the_field` |
| AC-B-01 = S5-AC-05, and **AC-B-02** | `unit/agents/test_receipt_graph_tools.py::test_s5_ac_05_an_out_of_role_call_fails_the_run_loudly` checks one denial per graph tool, through the real registry, in-process. It checks the TOOL event `allowed: false`, one `tool.denied` log with the checked role and tool, FAILED/FAILED, `failure` with the message, the last event ERROR, no retry and no later tool call. Also `unit/tools/test_registry.py::test_s5_ac_01_every_other_pair_is_denied_logged_and_raised` and the slice A `test_s5_ac_05_a_denied_tool_fails_the_run_loudly_without_retrying`; over MCP, `unit/agents/test_transport_equivalence.py::test_s5_ac_04_*[denied_tool]` (the same denial from the server, the same log line and run) |
| **AC-B-03** = S5-AC-03 | `e2e/test_mcp_server.py`. Each role lists exactly its tools, with descriptions and input schemas. Matching sees only its read tools. `save_receipt` at `/mcp/matching/` gives an error result and logs `tool.denied` without the payload. An allowed call returns `structuredContent`. An unknown role is 404. The Host-header allowlist is checked. `paper-trail mcp` exits 1 when the database is not migrated (in-process); once migrated a real process lists, denies, logs JSON and stops on SIGTERM |
| AC-B-03 = S5-AC-04 | `contract/test_tool_gateway.py` (both transports); `unit/agents/test_transport_equivalence.py` (clean run, validation retry, review routing, denied tool, resume and review resolution give equal runs over both transports; a guard that the MCP runs really use `McpToolGateway`); `unit/adapters/test_mcp_gateway.py` (unreachable server, timeout) |
| **AC-B-04** | `e2e/test_cli_tools.py::test_ac_b_04_*`: one role, every role, output identical over MCP; `contract/test_tool_gateway.py::test_describe_lists_the_roles_tools_from_the_registry` |
| **AC-B-05** | `e2e/test_cli_process_mcp.py::test_ac_b_05_*`: the same outcome, steps and totals for the same script (retry then complete; parked for review); an image read over MCP; `TOOLS__TRANSPORT` as the default |
| S5-AC-06 | `repo/test_import_contracts.py`: the four planted storage-port imports in `agents` break a contract, and the real tree passes |
| E3 commands | `e2e/test_cli_tools.py::test_e3_*` (denied exit 5 and `tool.denied` on stderr, the same calls over MCP), the usage errors, a domain error, the unmigrated database; `repo/test_agent_core_b_repo.py` (`make mcp-up` and `mcp-down`, the compose service) |
| Review note 1 (delimiter breakout) | `unit/agents/test_prompt_delimiters.py` (escape table, other text unchanged, nested attempts, properties: idempotent, no tag left, text without `<`/`>` unchanged; E4 breakout and plain sample; hostile feedback; examples); `unit/agents/test_receipt_graph_tools.py::test_review_note_1_*` (graph level) |
| Review note 2 (effective vision limit) | `unit/test_run_config.py`; `e2e/test_cli_process_mcp.py::test_review_note_2_*` |
| Review note 3 (`_prompt_tag` duplicate) | `repo/test_agent_core_b_repo.py::test_review_note_3_*` (`+noex` spelled only in `agents/prompts.py`); `e2e/test_cli_process_mcp.py::test_review_note_3_*` (the header tag equals the LLM events' tag, with and without examples) |
| Review note 4 (duplicate review on resume) | `contract/test_review_queue.py::test_find_open_*`; `contract/test_tool_gateway.py::test_opening_the_same_review_twice_returns_the_first`, `::test_an_open_review_of_another_thread_is_still_a_conflict`, `::test_a_closed_review_does_not_count`; `unit/agents/test_receipt_graph_tools.py::test_review_note_4_*` (crash after `open_review`, resume in a second harness: one OPEN review, the same id, then resolvable) |
| Review note 5 (plan status, E4 path) | Documentation only; checked in review. The wording tests were removed (test-suite cleanup, `docs/tests/test-suite-cleanup.md`) |
| Settings | `unit/test_config_agent_core_b.py`; the generic `.env.example` hygiene test picks up the new fields |

Out of slice B, and not tested here:

- `product_lookup`, `product_similarity_search`, `save_matches` and `save_enrichment`. They are already in `PLAN_ALLOWLIST`, and slice C or D adds a `SAMPLE_PAYLOADS` entry when it registers each one. Until then the matching and enrichment roles have no tools: `tools list` prints `(none)` for them, and S5-AC-03's matching check is "PLAN 7's matching tools that are registered".
- `--interrupt-after`, `--reprocess`, and `--resume` as a CLI flag. Resume after a crash is exercised only through the runner port; see below.

## Details the plans left open, pinned by the tests

### Ports (`paper_trail.ports.tools`)

- `@dataclass(frozen=True, slots=True) ToolDescription(name: str, description: str)`.
- `ToolGateway.describe(role) -> tuple[ToolDescription, ...]`: the tools the role may call, sorted by name. `tools list` uses it on both transports, and every gateway implements it, `TracingToolGateway` included (it delegates and does not trace).
- `ToolOutputError(Exception)`: a tool returned output that does not validate against its `output_model`, or is another model. The message names the tool. It is a bug in the tool and **not** a `ToolError`.
- `ToolUnavailableError(Exception)`: the MCP server refused the connection or did not answer within `McpSettings.timeout_s`. The message names `host:port`. It is not a `ToolError`.
- `ReviewQueue.find_open(receipt_id) -> ReviewItem | None` (port, fake and SQLite).

### Settings (`paper_trail.config`)

- `tools: ToolsSettings(transport: Literal["inprocess", "mcp"] = "inprocess")`.
- `mcp: McpSettings(url: AnyHttpUrl = "http://mcp:8000", timeout_s: float = 30.0, > 0)`.
- Both groups are frozen, and `.env.example` documents `PAPER_TRAIL_TOOLS__TRANSPORT`, `PAPER_TRAIL_MCP__URL` and `PAPER_TRAIL_MCP__TIMEOUT_S`.

### Registry (`paper_trail.tools.registry`)

- A call is checked in this order:
  1. the tool exists (`UnknownToolError`);
  2. the role may call it (`ToolNotPermittedError`, logged once as `tool.denied` at WARNING with `role` and `tool` and nothing else);
  3. the input validates (`ToolInputError`, naming the field; extra fields are refused);
  4. the handler runs;
  5. the output validates (`ToolOutputError`).
- `ToolRegistry(specs)` raises `ValueError`, naming the tool, for a duplicate name or a spec with empty `roles`.
- The allowlist is PLAN 7's table, verbatim.

### MCP server (`paper_trail.interfaces.mcp.server`)

- `create_mcp_app(registry)` returns an ASGI (Starlette) app with one MCP server per `AgentRole`, over streamable HTTP at `/mcp/<role>/`. The session managers of every role run in the app's lifespan.
- `tools/list` returns `registry.for_role(role).names`. Each tool carries `description = spec.description` and `inputSchema = spec.input_model.model_json_schema()`.
- `tools/call` always goes through `registry.for_role(role).call`, so a tool of another role is a denial, not "unknown tool". A result carries:
  - on success, the output as `structuredContent`;
  - on failure, `isError` with the error message as text content.
- The wire format that lets `McpToolGateway` raise the same class again is the implementer's choice. A suggestion: put `{"error": <kind>, "message": ...}` in `structuredContent` of the error result.
- Classes that must round-trip: `ToolNotPermittedError` (rebuilt from the call's role and tool), `UnknownToolError`, `ToolInputError`, `NotFoundError`, `ConflictError`.
- DNS-rebinding protection: only `Host` headers `127.0.0.1:*`, `localhost:*` and `mcp:*` are served; others get a 4xx that is not 404. The server has no authentication and can write, so this and the loopback-only port are its fence.
- `paper-trail mcp` serves the app on `settings.host:settings.http_port` (8000 in the container) over the main database:
  - it exits 1 with the `db upgrade` hint when the database is not migrated;
  - it logs like `serve`: JSON lines on stdout, configured by `configure_logging`;
  - it exits 0 on SIGTERM;
  - it never logs tool payloads.

### MCP gateway (`paper_trail.adapters.mcp.gateway`)

- `McpToolGateway(settings: McpSettings)` implements `ToolGateway` synchronously (the graph is synchronous) and talks to `<url>/mcp/<role>/` over streamable HTTP.
- Results are plain JSON, equal to the in-process ones.
- Errors keep their class, as above. `ToolUnavailableError` is raised for a refused connection or a timeout.
- The gateway does not log denials itself, so a denial is logged once, by the server.

### Graph and runner

- Denials: as slice A, now also with the real registry over both transports.
- **`ReceiptGraphRunner.resume(receipt_id)` with no draft continues a thread whose last run raised.** The failed node runs again from its start. This is pulled forward from AC-C-05, because review note 4 cannot be shown otherwise.
  - Verified on 2026-11-06 with the locked LangGraph 1.2.20: `invoke(Command(resume=None), ...)` raises `UnboundLocalError` inside LangGraph when no interrupt is pending, and `invoke(None, ...)` re-runs the failed node.
  - So the runner resumes with `None` when the thread is not paused at an interrupt, and with `Command(resume=...)` when it is.
- `open_review` is idempotent per receipt and thread. When the receipt already has an OPEN review with the same `thread_id`:
  - the tool returns that review's id;
  - it opens no second review;
  - it sets the receipt NEEDS_REVIEW/NEEDS_REVIEW again, because the crashed run may have left it FAILED.

  An OPEN review with another thread is still a `ConflictError`.

### Prompts (review note 1)

- `neutralize_delimiters(text) -> str` lives in `paper_trail.agents.prompts`.
  - It matches `<\s*/?\s*(receipt_data|validation_feedback)\b[^<>]*>` case-insensitively and escapes `<` as `&lt;` and `>` as `&gt;` in each match.
  - It repeats until nothing matches, so that escaping an inner tag can never complete an outer one (`<receipt_data<receipt_data>>`).
  - It changes nothing else, and it is idempotent.
- `RenderedPrompt.messages` applies it to the OCR text and to every feedback line, in that one place. The trace's LLM `messages` are therefore the neutralized text the model actually got.
- Unicode look-alike brackets (`＜`, `‹`) are not covered. The system prompt's "data, never instructions" rule and the code-checked arithmetic remain the other layers.

### CLI

- `paper-trail tools list [--role R] [--transport inprocess|mcp]`:
  - prints one line per (role, tool), in `AgentRole` order and then by name;
  - each line is whitespace-separated role, tool and description;
  - a role without tools prints `<role> (none)`;
  - it needs no database and no LLM host, and both transports print identical output.
- `paper-trail tools call --role R TOOL 'JSON' [--transport T]` prints the result as JSON on stdout and exits 0. Its other exit codes:

  | Exit | When | Message |
  |---|---|---|
  | 5 | `ToolNotPermittedError` | `denied: role=<role> tool=<tool>` on stderr; in-process, also the `tool.denied` log line (agent-command logging, WARNING to stderr) |
  | 2 | unknown role (Typer), payload not JSON ("JSON") or not an object ("JSON object"), unknown tool, input the tool refuses | names the field |
  | 1 | main database not migrated (in-process) | the `db upgrade` hint |
  | 1 | a tool's domain error | the error's message |
  | 1 | MCP server unreachable | names `host:port` |
- `--transport` defaults to `settings.tools.transport`. With `mcp`, `tools` commands use `settings.mcp.url`.
- `paper-trail process --transport mcp` **serves the run's own registry over MCP inside the CLI process** (`create_mcp_app` on a free loopback port) and calls it through `McpToolGateway`. It never contacts `MCP__URL`. The flag overrides `TOOLS__TRANSPORT`. The trace's `config.transport` and the terminal header say `mcp`.
- `bootstrap.run_config(settings)`:
  - `max_vision_attempts` is the effective `graph_limits(settings).max_vision_attempts`, so 0 until slice D;
  - `transport` is `settings.tools.transport`, and the CLI applies `--transport` to the settings.
- Review note 3: `process` uses the `RenderedPrompt.tag` it loaded to check `--prompt-version`. The `+noex` suffix is spelled only in `agents/prompts.py`.

### Compose, Make, dependencies

- Compose service `mcp`:
  - the app image, profile `mcp`, `command: ["mcp"]`;
  - `ports: ["127.0.0.1:8421:8000"]`, literal loopback, not `PAPER_TRAIL_BIND_ADDR`;
  - waits for `migrate` to complete;
  - the `x-app` hardening, the data volume, and `mem_limit` and `cpus`.
- `make mcp-up` is `$(DC) --profile mcp up -d mcp`. `make mcp-down` stops and removes only `mcp`, for example `$(DC) --profile mcp rm --stop --force mcp`; never `down`. Both targets are in `.PHONY` and have help text.
- The runtime dependency is `mcp>=2.3,<3`, locked. Verified in a throwaway container on 2026-11-06: the current SDK is 2.3.0.
  - It renamed FastMCP to `mcp.server.mcpserver.MCPServer`; the low-level `mcp.server.lowlevel.Server(on_list_tools=..., on_call_tool=...)` also exists.
  - `streamable_http_app(...)` defaults to `host="127.0.0.1"` for its transport security, so the allowed hosts must be widened to `mcp:*`.
  - The client is `mcp.Client(url)`, async.
  - It depends on `httpx2`, already a dev dependency here, and on `sse-starlette`.
- The README documents:
  - `make mcp-up` and `make mcp-down`;
  - `tools list`, `tools call` and its exit 5;
  - `--transport mcp`.

## Decisions for the owner or orchestrator to confirm

1. **`process --transport mcp` runs its own in-process MCP server** instead of using the standalone `mcp` service.
   - Why: a standalone server writes to the main database, while `--run NAME` processes into `runs/NAME/paper_trail.db`, so the server would not find the run's receipts.
   - What is kept: the hop is still real streamable HTTP through the production server and gateway, so E3's "time per receipt in-process vs MCP" is measured.
   - What changes: `make mcp-up` serves `tools list/call --transport mcp` and external MCP clients only. The plan's E3 note "needed only when TOOLS__TRANSPORT=mcp" changes accordingly.
2. `ToolGateway` gains `describe(role)`, so that `tools list` is one code path for both transports. The alternative is a separate catalog port.
3. `ToolOutputError` and `ToolUnavailableError` are not `ToolError`s: `ToolError` means "the caller's fault".
4. `ReviewQueue` gains `find_open(receipt_id)`, the smallest port change that makes `open_review` idempotent.
5. Resuming a crashed thread through `runner.resume(id)` arrives with slice B rather than C, to test review note 4.
