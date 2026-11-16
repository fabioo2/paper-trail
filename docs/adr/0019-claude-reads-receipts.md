# 19. Claude reads receipts; no OCR, no local models, no vectors

Date: 2026-11-15

## Status

Accepted

## Context

The local stack (Tesseract OCR, then Ollama models on a GPU PC; ADR-0009, ADR-0010, ADR-0016) needed a store-specific prompt fix for each new receipt layout, took about three minutes per photo and failed whenever the GPU PC was off. A trial on the stored receipts plus a new Metro receipt, with Claude Haiku answering in structured JSON and the same arithmetic scoring, matched every stored value on three of four receipts, read Metro's "already taken off" savings correctly at the first attempt, and its one misread (a smudged price) was caught by the arithmetic check. Matching has used product codes, not embeddings, since ADR-0018, so the vectors no longer earn their place.

## Decision

1. **One Claude call reads the photo** (`ReceiptReader`, `adapters/anthropic`): the photo, upright and at most 2576 px on its long edge (a JPEG whose base64 fits the API's 5 MB), and a short prompt, with a structured-output schema of finished items: per item its printed text, code, department, quantity, unit, unit price, regular price, discount and price paid, plus the receipt-level adjustments (deposits, fees, order discounts). Amounts stay strings as printed; code converts and checks them (ADR-0017 kept).
2. **Escalation, not a loop.** Attempt 1 runs on `CLAUDE__EXTRACT_MODEL` (Haiku 5.5). When the code check fails (each item's regular price minus discount is its price paid; a weighed item's quantity times unit price matches its regular or paid price within a cent; the receipt sums), attempt 2 runs on `CLAUDE__RETRY_MODEL` (Opus 5.5) with the failures listed; then a person reviews. A refusal, a cut-off answer or an API error fails the receipt with its checkpoint, to resume.
3. **Storage is unchanged.** A mapper writes each item as an ITEM line plus a linked DISCOUNT line (ADR-0018's price history works as before); the item's code replaces parsing codes out of the printed text.
4. **The graph keeps its shape and its tools** (ADR-0008, ADR-0013): nine nodes, `extract, validate, supervisor, review, persist, match, commit_matches, enrich, commit_enrichment`. The OCR tool and `product_similarity_search` are removed.
5. **No vectors.** Migration 0007 drops `product_vectors` and its trigger; sqlite-vec is loaded only by the migration runner, so an empty database still migrates through 0002.
6. **A monthly budget** (`CLAUDE__MONTHLY_BUDGET_USD`, default 5.00, UTC month) is checked before each call; each call's tokens and cost are recorded in `claude_calls`.

## Consequences

- The whole photo of a receipt is sent to Anthropic, with whatever it shows (card digits as printed, store, loyalty id, sometimes a name); ADR-0015's "nothing leaves your hardware" no longer holds for photos. Card numbers in the answer are still masked before anything is stored or traced (ADR-0011).
- The GPU PC, `deploy/llm`, the single-host overlay and Tesseract in the image are gone; an `ANTHROPIC_API_KEY` in `.env` is the only setup.
- Checkpoints saved by the old version cannot be resumed (state version 3); their reviews are answered with a corrected file, or the photo is read again.
- Tests never call the API: fakes stand behind `ReceiptReader`; `make smoke-claude` is the opt-in live check.
