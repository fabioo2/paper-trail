# 9. Ollama via OpenAI-compatible endpoint with JSON-schema output

Date: 2026-11-06

## Status

Accepted

## Context

Models run on a desktop RTX 3080 (10 GB VRAM) through Ollama: Qwen2.5 7B instruct (text), Qwen2.5-VL 7B (vision), nomic-embed-text (embeddings). Outputs must match fixed schemas.

## Decision

- `LlmPort.complete_structured(request, schema)` returns a validated Pydantic model. The Ollama adapter calls `/v1/chat/completions` with `response_format={"type": "json_schema", ...}` generated from the Pydantic model, temperature 0, using `httpx` (no vendor SDK).
- Responses are validated with Pydantic; one schema-repair retry is allowed, then `LlmOutputError`.
- `EmbeddingPort` calls `/v1/embeddings`.
- If the installed Ollama version rejects `json_schema` on the compatible endpoint, the adapter uses the native `/api/chat` `format` parameter instead; the port does not change.
- Only one model is resident at a time on 10 GB VRAM; the worker processes one receipt at a time and sets `keep_alive` from settings.

## Consequences

- Adapter tests use `respx` with recorded payloads; no GPU needed for `make check`.
- Switching providers means one new adapter.

## Amendment (2026-11-06): agent core and measurements

Measured on the GPU PC (Ollama 0.40.15): Ollama's default context is 4096 tokens, the text model returned dates as `MM/DD/YYYY` even though the schema description asked for ISO, nomic-embed-text returns 768-dim vectors, and only one model is loaded at a time (`OLLAMA_MAX_LOADED_MODELS=1`, so a model switch costs about 7 s).

- The adapter uses Ollama's native `/api/chat` (`format` = the JSON schema, `options.temperature`, `options.num_ctx`) instead of the OpenAI-compatible endpoint, which cannot set the context length. `num_ctx` comes from one setting (`OLLAMA__NUM_CTX`, default 8192), because changing it between requests reloads the model. Embeddings use native `/api/embed`.
- `complete_structured` returns `LlmResult[M]` (`value`, `prompt_tokens`, `completion_tokens`) so token usage reaches the trace, and `LlmRequest` gains `tag` (the prompt id, e.g. `extraction/v1`).
- Schema descriptions are not trusted to enforce formats: dates are strings in the LLM schema and are parsed and validated in code (`domain/rules/dates.py`); money stays integer cents enforced by the schema type.

## Amendment (2026-11-06): agent core slice C, as built

- `OllamaEmbeddings` (`adapters/ollama/embeddings.py`) sends one `POST /api/embed` per batch with the embed model and `keep_alive`. An answer with another number of vectors than texts, or vectors that are not 768 long, is an `LlmOutputError` (the configured model is not the one the vector index was built for); connection failures, timeouts and error statuses are `LlmUnavailableError` naming the host, so a GPU that drops during matching stops the run with its checkpoint, like one that drops during extraction. Both adapters share `adapters/ollama/http.py` for the client and that error mapping.
- The matching confirmation (`matching/v1`) uses the text model, so a receipt's matching costs no model switch after its extraction; embedding calls do load `nomic-embed-text` (about 7 s when the text model was loaded). A line found by correction memory or an exact alias makes no embedding call at all.

## Amendment (2026-11-08): amounts as printed (ADR-0017)

The `extraction/v2` and `vision/v1` schemas (`PrintedExtraction`) ask for amounts, quantities, unit prices, taxes and the date as the strings printed on the receipt ("4.00-", "0.460 kg", "26/11/02"), not integer cents: the vision model wrote 1.29 as 1290 cents and dropped trailing minus signs. Pure domain parsers (`domain/rules/printed.py`) compute the cents, signs and quantities, and `PrintedExtraction.to_output()` returns the existing `ExtractionOutput`, so every later step is unchanged. `extraction/v1` (integer cents) stays for comparison. See ADR-0017.
