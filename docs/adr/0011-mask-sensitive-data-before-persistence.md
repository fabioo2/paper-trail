# 11. Mask sensitive data before persistence

Date: 2026-11-06

## Status

Accepted

## Context

Receipts can contain full or partial card numbers, and phone photos carry EXIF metadata including GPS location.

## Decision

- `domain.rules.masking.mask_pans` finds 13 to 19 digit sequences (spaces or dashes allowed), and masks every Luhn-valid one except its last four digits. It runs on OCR text and on every extracted text field before any repository call, inside the use case, so no adapter ever sees an unmasked PAN.
- Stored images are re-encoded without EXIF (orientation applied first). The original upload bytes are never written to disk.
- Logs never include OCR text or raw LLM payloads above DEBUG, and DEBUG is off in production.

## Consequences

- Rare false positives (a Luhn-valid barcode gets masked) are harmless.
- The original photo cannot be recovered with metadata; accepted.

## Amendment (2026-11-06): agent core slice A

Card numbers are masked where text enters the system, before it reaches graph state, checkpoints, prompts, traces or the database: in the `ocr_read` tool (photos), in `ProcessReceiptFile` (OCR text from a file, before the receipt row exists), in `ResolveReview` (a person's draft, before it enters the graph) and in `RecordReceipt` (every save). The tracer masks every summary and data string again as defence in depth. LangGraph's SQLite checkpointer appends sixteen random digits to channel versions, which a Luhn scan cannot tell from a card number; the app uses a subclass with plain counters (`adapters/sqlite/checkpoints.py`) so that the database can be audited for card numbers.
