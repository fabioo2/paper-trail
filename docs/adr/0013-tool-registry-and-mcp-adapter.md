# 13. Single tool registry with per-role allowlists; MCP as an adapter

Date: 2026-11-06

## Status

Accepted

## Context

The proposal serves tools through an MCP server and requires that each agent can only call the tools its role needs.

## Decision

- Tools are plain typed functions with Pydantic input and output models, declared once in `tools/registry.py` as `ToolSpec`s with the roles allowed to call them.
- `ToolRegistry.for_role(role)` returns a `RoleToolbox`; calling a tool outside the role raises `ToolNotPermittedError` (logged and surfaced; never silently ignored).
- Agents depend on a `ToolGateway` port. Adapters: in-process (default, used in tests) and MCP client. The FastMCP server in `interfaces/mcp/` exposes one endpoint per role that lists and serves only that role's tools, and still checks the registry.

## Consequences

- One definition per tool; the MCP layer cannot drift from the in-process behaviour.
- Allowlist enforcement is tested once at the registry and again at the MCP boundary.

## Amendment (2026-11-06): agent core

- `AgentRole` lives in `domain/enums.py` (ports and trace events need it); `tools/registry.py` imports it.
- Nodes call tools through `ToolGateway` from the first agent-core slice, and the registry enforces roles from day one, so hardening the allowlist and adding MCP later needs no node refactor.
- Specialists only read. Every write is a supervisor tool: `save_receipt`, `open_review`, `save_matches` (links lines, creates products, MATCHED aliases, vectors and price observations in one unit of work) and `save_enrichment`. `product_lookup` checks correction memory before aliases and returns which one matched.
- Every tool call, including a denial, is recorded as a trace event.

## Amendment (2026-11-06): agent core slice B, as built

- The MCP SDK 2.x renamed FastMCP to `MCPServer`; the server uses the SDK's low-level `Server`
  instead (`interfaces/mcp/server.py`), because it lists and calls the registry's specs as they
  are, with no per-tool decorators to keep in step. `create_mcp_app(registry)` mounts one
  stateless streamable-HTTP server per `AgentRole` at `/mcp/<role>/`; `tools/call` always goes
  through that role's toolbox, so another role's tool is a denial, not an unknown tool.
- Errors cross the wire as structured error results (`tools/wire.py`); `McpToolGateway`
  raises the same class again (`ToolNotPermittedError`, `UnknownToolError`, `ToolInputError`,
  `NotFoundError`, `ConflictError`, ...). Denials are logged once, by the registry, wherever it
  runs. `ToolOutputError` (a tool returned something else than its output model) and
  `ToolUnavailableError` (no MCP server) are not `ToolError`s: the caller did nothing wrong.
- `ToolGateway.describe(role)` lists a role's tools, so `paper-trail tools list` is one code
  path over both transports.
- The standalone server (`make mcp-up`, `paper-trail mcp`) has no authentication and its
  supervisor tools write: it is published on `127.0.0.1:8421` only and serves only the Host
  names `127.0.0.1`, `localhost` and `mcp` (DNS-rebinding protection).
- `paper-trail process --transport mcp` serves its run's own registry on a loopback port inside
  the command and calls it through `McpToolGateway`: a `--run` writes to its own database, which
  the standalone server does not see. The hop is still real streamable HTTP through the
  production server and gateway.
- `import-linter` contract 4b keeps `agents` away from the storage ports (repositories, unit of
  work, queues, search): the allowlist is the whole of what a role can reach (S5-AC-06).

## Amendment (2026-11-06): agent core slice C, as built

- Tools `product_lookup` (matching, enrichment), `product_similarity_search` (matching) and
  `save_matches` (supervisor). `product_similarity_search` answers `{candidates: [...]}`, not a
  bare list, because an MCP structured result is an object.
- `save_matches` embeds the new products' aliases before it opens its writing transaction (a
  slow embedding host must not hold SQLite's write lock), then writes everything in one unit
  of work; it is idempotent, like `open_review`, because a crash between its commit and the
  graph's checkpoint makes the resumed run call it again.
- `tools/wire.py` carries `LlmUnavailableError` and `LlmOutputError` (kinds `llm_unavailable`,
  `llm_output`): the embedding host failing inside a tool stops the run with its checkpoint over
  MCP too, instead of failing the receipt.
- `McpSettings.timeout_s` bounds one call, and a call waits for its tool, so `Settings` refuses
  an `MCP__TIMEOUT_S` that is not greater than `OCR__TIMEOUT_S` and `OLLAMA__REQUEST_TIMEOUT_S`
  (default 240 s). `McpToolGateway` names the scheme's default port (80 or 443) in its
  messages, and reads a foreign server's text-only result as one JSON object (anything else is
  a `ToolOutputError`, since the server did answer).
