# 17. The LLM transcribes, code computes

Date: 2026-11-08

## Status

Accepted

## Context

The first real photos (a crumpled Costco and a Superstore receipt, 2026-11-08) never completed. Each failure was a number the model was asked to compute: the vision model wrote 1.29 as 1290 cents, returned a printed `4.00-` instant saving as a positive item, and read the Superstore date `26/11/02` as 2022-11-26. On a poor OCR read both models copied few-shot example 05 (`WALMART`, `GV PEANUT BUTTER`) instead of reading the receipt, text retries made answers worse, and the numbers the text model invented anchored the vision attempt that followed. Promo text (`$1.29 lmt 4`, `3/$9.99`) was read as a quantity or unit price. Plan: `docs/plans/extraction-accuracy.md`.

## Decision

- **Values as printed.** The `extraction/v2` and `vision/v1` prompts answer `PrintedExtraction`: amounts, quantities, unit prices, every tax line and the date as the strings printed. Pure parsers in `domain/rules/printed.py` (no framework imports) compute signed integer cents (`parse_amount`: two fraction digits exactly, sign from a leading or trailing minus, `CR` or parentheses, tax-flag letters ignored), `Decimal` quantities (`parse_quantity`), lenient unit prices (`parse_unit_price`: optional, unreadable is None, `N/$X` is X/N half-up) and the kind (`line_kind`: a negative line is a DISCOUNT). Two guards fix what the real photos showed: a line referring to another item's code (`0000392208 / 1362394`, `is_item_reference`) is a DISCOUNT even when its faint minus was missed, and a printed unit price equal to the printed amount means quantity 1 (`line_quantity`: a size in the name, `KIMCHI 1.5KG`, is not a quantity). Code sums the taxes. `PrintedExtraction.to_output()` returns the existing `ExtractionOutput`, so validation, the supervisor, drafts, tools, review, persist and matching are unchanged. Every unreadable value is named in one `PrintedValueError` that quotes only the printed text; it becomes the next attempt's feedback.
- **Multi-buy over several products.** A run of lines without an amount followed by a priced line whose quantity equals the group size shares that amount evenly (remainder on the last line, `share_amounts`); any other missing amount is an error, so the model cannot hide an unreadable price.
- **Two-digit-year dates** (`domain/rules/dates.py`): readings are tried M/D/Y, D/M/Y, Y/M/D; the first one not in the future and at most `RECENT_DAYS` (300) old wins, else the first one not in the future. A trailing time is ignored. The window is below a year on purpose: a YY/MM/DD date printed on day YY-1 (`26/11/25`) also reads D/M/Y as a date 364-365 days earlier, which must not win. A module constant, not a setting: receipts are processed within days.
- **Few-shot leak guard.** v2 examples use only placeholder content (`SAMPLE MART #0000`, `SAMPLE ITEM A`, card `0000`), and the extraction node treats an answer whose store name or any line text (after `normalize_alias`) equals an example's, or two or more of whose lines do, as unparsed (one line alone is no copy: v1's examples print real products such as BANANAS). Generic lines (`INSTANT SAVINGS`, `DEPOSIT`) never count, since real receipts print them. The guard covers v1 too.
- **Vision on its own terms.** `vision/v1` sends the photo alone: no examples, no OCR text. A photo whose OCR mean confidence is below `GRAPH__VISION_FIRST_BELOW` (0.65) goes to vision first with no text attempts (`effective_limits`, `first_attempt` in `agents/routing.py`, used by both the `read` edge and the supervisor). The first vision attempt gets a clean prompt; a vision retry (`GRAPH__MAX_VISION_ATTEMPTS` now 2) gets only the issues of the previous vision answer.
- **Item codes are not sizes.** `split_item_code` drops a leading 4-13 digit code from the line text the enrichment model sees (`1281 1% MILK` was read as 1281 ml); aliases keep the full text.
- Defaults: `OCR__PSM` 6, `PROMPTS__EXTRACTION_VERSION` v2, new `PROMPTS__VISION_VERSION` v1. `extraction/v1` is unchanged, for the comparison run (`--prompt-version v1`).

## Alternatives considered

- **Prompt-only fixes** ("answer in cents, keep the sign, ISO dates"): v1 already said all of that and the models still got it wrong; a 7B model is a poor calculator and a good copier.
- **Removing the examples from the text prompt entirely**: the examples still teach the shape of the hard cases (trailing minus, weighed lines, multi-buy); placeholder content plus the guard keeps that without the leak.
- **A tolerance for weighed lines and `3/$10.00` prices**: unnecessary; the existing 1-cent per-line tolerance covers 0.460 x 3.31 = 1.5226 and 3 x 3.33 = 9.99.

## Consequences

- The numbers on a saved receipt are computed by tested code; a model error surfaces as a named, quoted value in the retry feedback rather than a silently wrong amount.
- Two schemas exist for one output; `ANSWER_MODELS` maps each extraction prompt to its answer model, and an extraction prompt version without one fails when the graph is built.
- The vision-first threshold is calibrated on two photos only; the experiment trace records each receipt's OCR confidence and route so it can be moved.
- Deferred: attributing a discount to the item it reduces (needs a net-price column and an owner decision on which price the history shows); `split_item_code` is its first building block.

## Amendment (2026-11-08): the photo upright, and a bounded answer

An unattended `process` on the owner's iPhone photos stopped the whole run. OCR read one photo `rotated 90°` (confidence 0.64) and sent it vision first, but the stored photo is still sideways: the OCR's rotation was only applied inside the OCR adapter. On the sideways photo, `qwen2.5vl:7b` looped (the same "CRV CRN SD BRRT" line over and over) up to `num_predict` 4000 (`done_reason` "length"). No request set `num_predict`. The adapter's JSON-repair round then re-sent the whole answer plus the image, and the LLM host answered 400 "request (12383 tokens) exceeds the available context size (12288 tokens)" after 84 s. That surfaced as `LlmUnavailableError`, which stops the run. The same photo turned upright gave a clean 1417-token answer in 22 s.

- The `read` node keeps the OCR's `rotation_degrees` in the state (`ocr_rotation`; absent in older checkpoints, so 0). The vision attempt opens the photo through `ImageStore.open_upright(path, degrees)`, which turns it counter-clockwise like the OCR adapter (a lossless quarter-turn transpose) and re-encodes it as JPEG. Pillow stays in the adapter, behind the port.
- Every extraction and vision request sets `max_tokens` (`num_predict`) from `PAPER_TRAIL_OLLAMA__MAX_OUTPUT_TOKENS` (default 4096).
- An answer cut off at that bound that does not validate is an `LlmOutputError` with no repair round. Ollama's 400 "exceeds the available context size" is an `LlmOutputError` too, so the supervisor routes it to a retry, vision or review instead of stopping the run. Other error statuses and connection failures still raise `LlmUnavailableError`.

The 4096-token default fits about 50 receipt lines (the 22-line Costco answer was about 1400 tokens). `Settings` refuses a `MAX_OUTPUT_TOKENS` that is not below `NUM_CTX`, because the prompt and the photo need room too. Any 400 whose reason mentions the context counts as an overflow, since Ollama words it more than one way.

## Amendment (2026-11-08): reconciling what vision still gets wrong

An unattended run on the original photos completed Costco with all 22 lines right. Superstore went to review, with the same two errors on both vision attempts:

- **A tax that is not printed.** Vision returned taxes `["8.99 H"]` although the subtotal 99.40 equals the total 99.40. `printed_tax` now makes the tax 0 whenever the printed subtotal equals the printed total.
- **A missed multi-buy.** Vision gave both products of `2 @ 2/$10.00  10.00` the offer's 5.99 (from `$5.99 ea or 2/$10.00`), so the items came to 101.38 against the 99.40 subtotal, 1.98 over. Tesseract's text has the multi-buy line. `reconcile_multi_buy` (in `domain/rules/reconcile.py`, pure) is exact or nothing. It needs:
  - one OCR line "N @ N/$X  X";
  - one run of N adjacent ITEM lines with equal totals p;
  - items minus subtotal equal to N*p - X.

  When all three hold, the run takes X split like `share_amounts`; otherwise nothing changes. The extraction node applies it to every answer and records a `note` trace event, "reconciled multi-buy 2 @ 2/$10.00 from OCR", so the fix is never silent (a zeroed tax gets a `note` too: "tax 8.99 H ignored: subtotal equals total"). The OCR line is matched tolerating Tesseract misreads ("2 @ 2/S10.00", "2 @ 2 / $10.00"). A blind test on a new Superstore receipt (8 items, subtotal 28.70, total 29.54) read every line, the subtotal, the total and the date right on all four attempts, but the tax line `H=HST 13%    6.49 @ 13.000%     0.84` came back as "6.49 H", "0.84 13%", "6.49 13%" and "6.49 @ 13.000%", and `parse_amount` refused them. `parse_tax` now drops rates and tax names, takes the amount printed after "base @ rate%", or else computes base x rate rounded half-up (with a `note`: "tax 0.84 computed from 6.49 @ 13.000%"), and otherwise uses the one amount left. Both prompts now ask for the tax amount at the right edge, never the base or the rate. `TraceKind` gains `NOTE`, and the trace schema is regenerated to include it.

## Amendment (2026-11-08): every photo goes vision first; incomplete is not COMPLETED

A blind re-run of a new Superstore receipt went text first (OCR confidence 0.68, above the 0.65 threshold). It completed with all 8 amounts and the tax right, but with store "GEAL CANADIAN" (the logo misread by OCR), no date and a wrong card number. The receipt therefore got 0 price observations, which need a date, and a store key no alias matches. The vision path on the same photo read "Real Canadian SuperStore" and "26/11/07", and the card number correctly. Across the three real receipts of the day, the text path never produced fully correct data. With the owner's approval:

- **Vision first for every photo.** `GRAPH__VISION_FIRST_BELOW` now defaults to 1.0, and 1.0 is a dedicated rule (`ALWAYS_VISION_FIRST`): every photo goes vision first, even one whose OCR read is perfect or has no confidence. OCR still runs, for the rotation, card masking and the multi-buy cross-check. A text input (`--text-file`) still uses the text path, and `--no-vision` or 0 still turns vision first off.
- **Completeness beside arithmetic.** `check_completeness` (`domain/rules/completeness.py`, pure) reports a blank store name (`MISSING_STORE`) and a purchase date that is missing or reads as no date up to today (`MISSING_DATE`). Its messages are feedback the model can act on: "no purchase date was read: copy the date exactly as printed" and "no store name was read: copy the store name printed at the top". The `validate` node adds these issues to the arithmetic ones, so they route the same way: a retry with feedback, then review with reason `validation_failed`. Nothing incomplete is saved as COMPLETED by the graph, but a person can still keep such a receipt from the review (`review resolve`, `--force`), because saving checks the arithmetic only. `IssueCode` gains the two codes, so the OpenAPI spec and the generated client were regenerated.
