# Extraction accuracy plan (real photos -> COMPLETED)

Written 2026-11-08 by the planner. Status: **approved by the owner, 2026-11-08.** Builds on `docs/plans/agent-core.md` (slices A-D, as built). Deadline: tonight; budget ~1.5 h implement + review.

## Summary (approve from here)

1. Principle: **the LLM transcribes, code computes.** A new extraction schema (`extraction/v2`) returns amounts, quantities, unit prices, tax lines and the date **as printed** (strings); pure domain parsers turn them into signed integer cents, `Decimal` quantities and a `date`. This removes the 10x cents bug, the `4.00-` sign/kind bug and the `26/11/02` date bug in code, not in the prompt.
2. The converted answer is the existing `ExtractionOutput` JSON, so `validate`, `supervisor`, drafts, tools, review, persist and matching are **unchanged**. Only the extraction node, the date rule, routing and prompts change.
3. Few-shot leak fix: v2 text examples use obviously fake placeholder content (`SAMPLE MART`, `SAMPLE ITEM A`) and a code guard rejects any answer that copies an example's store or line text. Vision gets its own prompt `vision/v1` with **no examples and no OCR text**.
4. Routing: OCR mean confidence below `GRAPH__VISION_FIRST_BELOW` (default 0.65) on a photo -> **vision first, no text attempts**. A vision attempt that follows text attempts gets a **clean prompt** (no feedback); a vision retry gets only issues computed from the vision model's own previous answer.
5. Defaults: `OCR__PSM` 4 -> 6, `PROMPTS__EXTRACTION_VERSION` v1 -> v2, `GRAPH__MAX_VISION_ATTEMPTS` 1 -> 2. v1 stays for the E1-style comparison (`--prompt-version v1`).
6. Two-digit-year dates: prefer the reading (M/D/Y, then D/M/Y, then Y/M/D) that is not in the future and within 366 days of today; `26/11/02` -> 2026-11-02.
7. Multi-buy sharing one printed amount (Superstore `2 @ 2/$10.00  10.00` over two products): **stretch S1**, a guarded split in code. If cut, Superstore may still go to review; **success = Costco COMPLETED, Superstore COMPLETED is the stretch.**
8. No new rounding tolerance: the existing 1-cent per-line tolerance covers weighed items (0.460 x 3.31 = 1.5226 -> 1.52) and `3/$10.00` unit prices (3 x 3.33 = 9.99 vs 10.00).
9. Addendum: item-code-as-size (enrichment read `1281 1% MILK` as 1281 ml) is **stretch S2** (small, code-side split). Discount attribution to the item it references is **deferred** (needs a migration and a pricing-semantics decision).
10. New ADR-0017; amendments to ADR-0008, ADR-0009, ADR-0010. Cut order if late: S2, then S1, then the leak guard (keep the placeholder examples).

---

## 1. Evidence -> fix map

| # | Evidence (2026-11-08 real runs) | Fix | Where |
|---|---|---|---|
| 1 | psm 4 drops the right price column on crumpled paper; psm 6 recovers it | default `OCR__PSM=6` | config, ADR-0010 |
| 2 | Text and vision copied example 05 (`WALMART`, `GV PEANUT BUTTER`) on poor OCR | placeholder-only v2 examples + leak guard; vision prompt without examples | prompts, extraction node |
| 3a | Vision: 1.29 -> 1290 cents | amounts as printed strings, cents computed in code; strict "two fraction digits" rule | `domain/rules/printed.py` |
| 3b | `4.00-` returned as `item`, +400 | sign read from the printed text (trailing `-`, leading `-`, `CR`, parentheses); negative or `discount` -> DISCOUNT, negative | same |
| 3c | `26/11/02` -> 2022-11-26 | recency-preferring two-digit-year rule; trailing time tolerated | `domain/rules/dates.py` |
| 3d | Multi-buy one amount over two products | S1 guarded split, else review | `domain/rules/printed.py` |
| 4 | Text retries worsened answers; text-invented numbers anchored vision | vision-first on low OCR; clean vision prompt after text attempts | routing, supervisor |
| 5 | Promo text (`$1.29 lmt 4`, `3/$9.99`) read as quantity/unit price | v2 rule: quantity/unit price only from an `N @ price` or weight line; unit price optional and parsed leniently | prompt, parser |

Tax: v2 returns `taxes: [printed amounts]`; code sums them (the model never adds).

## 2. Design

### 2.1 Schema v2 (model output, `agents/schemas.py`)

```python
class PrintedLine(BaseModel):          # extra="forbid", frozen
    raw_text: str                       # as printed, without the price
    description: str                    # readable name, never an item code/UPC
    quantity: str | None                # as printed: "2", "0.460 kg", null -> "1"
    unit_price: str | None              # as printed: "1.29", "$3.31/kg", "2/$10.00", null
    amount: str | None                  # as printed: "1.29", "4.00-", "-4.00", "1,29", "4.00 CR"; null only for S1
    kind: LineKind                      # item | discount | deposit | fee (sign is derived in code)

class PrintedExtraction(BaseModel):
    store_name: str; purchased_on: str | None   # date text as printed, e.g. "26/11/02"
    currency: CurrencyCode; subtotal: str | None; taxes: list[str]; total: str
    payment_last4: Last4 | None; items: list[PrintedLine]; confidence: float (0..1)
    def to_output(self) -> ExtractionOutput     # raises PrintedValueError listing every bad field
```

- `ExtractionOutput` (v1) gains a trivial `to_output()` returning itself, so both satisfy one protocol `ExtractionAnswer` (`to_output() -> ExtractionOutput`).
- `ANSWER_MODELS: Mapping[str, type[ExtractionAnswer]] = {"extraction/v1": ExtractionOutput, "extraction/v2": PrintedExtraction, "vision/v1": PrintedExtraction}`, keyed by prompt id without `+noex`; an unknown id fails at graph build (`LookupError`).
- `PrintedValueError` messages name the field and quote the printed string only, e.g. `line 3 amount "1290": write the amount exactly as printed, with its cents, e.g. "12.90"`. They become the retry feedback via the existing `UNPARSED_RETRY` path; no invented numbers.

### 2.2 Domain parsers (`domain/rules/printed.py`, pure, no pydantic)

- `parse_amount(text) -> int` signed cents. Accepts optional `$`, spaces, thousands `,`, decimal `.` or `,` with **exactly two** fraction digits, trailing tax flag letters (`A`, `H`, `GST`, `MRJ`: 1-3 letters, ignored). Negative when: leading `-`/`−`, trailing `-`, trailing `CR`, or `( )`. Otherwise `PrintedValueError`.
- `parse_quantity(text | None) -> Decimal > 0`. None/blank -> 1; strips `kg`, `lb`, `ea`, `@`, `x`.
- `parse_unit_price(text | None) -> int | None`, **lenient** (unparseable -> None; it is optional and only feeds the line-math check): strips `$`, `@`, `ea`, `/kg`, `/lb`; `N/$X` -> X/N rounded half-up.
- `line_kind(kind, cents) -> (LineKind, cents)`: negative or `discount` -> `(DISCOUNT, -abs(cents))`; otherwise as given (a negative `item` cannot survive).
- S1 `share_amounts(lines)`: a run of k >= 1 lines with `amount` null followed by a priced line whose quantity == k+1 becomes k+1 lines with quantity 1, unit price None, the amount split evenly (remainder cents on the last line). Any other null amount -> `PrintedValueError("line N: no amount printed")`. The guard (quantity == group size) stops the model from hiding unreadable prices.

### 2.3 Dates (`domain/rules/dates.py`)

For `A/B/YY` (all two-digit): candidates in preference order M/D/Y, D/M/Y, Y/M/D; pick the first valid one that is not in the future and within `RECENT_DAYS = 366` of `today`; if none, the first valid non-future one (= today's behaviour). Four-digit forms unchanged. An optional trailing time (`HH:MM[:SS][ AM|PM]`) is ignored. v1 benefits too. Module constant, not a setting (receipts are processed within days; documented in ADR-0017).

### 2.4 Extraction node (`agents/nodes/extraction.py`)

- Loads `extraction/<PROMPTS__EXTRACTION_VERSION>` for text and `vision/<PROMPTS__VISION_VERSION>` for vision; picks the answer model from `ANSWER_MODELS` by tag.
- After `complete_structured`: `answer.to_output()`; then the leak guard: if the output's `store_name` or any line `raw_text` (normalized with `normalize_alias`) equals one from the prompt's examples -> treated as unparsed with `parse_error = "the answer copied the example receipt; read the receipt in <receipt_data>"`. `RenderedPrompt` gains `example_texts() -> frozenset[str]` (store names + raw_texts of the example outputs; works for v1 and v2 example shapes).
- State keeps `ExtractionOutput` JSON (unchanged contract). The LLM trace already records the printed answer.

### 2.5 Prompts

- `extraction/v2/`: `system.md` (transcribe, do not compute: copy every number exactly as printed incl. `-`/`CR`; date as printed; quantity/unit price only from `N @ price` or weight lines, never from promo/limit text like `lmt 4` or `3/$9.99`; description never contains item codes/UPCs; multi-buy rule for S1), `user.md` (as v1), `schema.json` (generated from `PrintedExtraction`), `examples/`: `01-sample-discount-multibuy.json` (trailing-minus discount, `2 @ 2/$5.00` shared amount), `02-sample-weighed-yymmdd.json` (`0.460 kg @ $3.31/kg  1.52`, date `26/10/14`), `03-sample-injection.json`. Every store is `SAMPLE MART #0000` / `SAMPLE GROCER`, every item `SAMPLE ITEM A..`, card `0000`.
- `vision/v1/`: `system.md` (same rules, reading the attached photo), `user.md` = `{feedback_block}Read the attached receipt photo and answer with the JSON object.` (no `{ocr_text}`, no examples dir), `schema.json` = v2's.
- v1 files untouched.

### 2.6 Routing (`agents/routing.py`, `agents/graph.py`, `agents/nodes/supervisor.py`)

```python
@dataclass(frozen=True, slots=True)
class GraphLimits: ...; vision_first_below: float   # new; 0 disables

def effective_limits(limits, *, has_photo: bool, ocr_confidence: float | None) -> GraphLimits:
    # no photo -> max_vision_attempts=0 (replaces supervisor's `without_photo`)
    # photo, vision allowed, ocr_confidence < vision_first_below -> max_text_attempts=0
def first_attempt(limits: GraphLimits) -> Target   # "extract_text" if max_text_attempts else "extract_vision"
```

- `route()` is unchanged (with `max_text_attempts=0` it already goes to vision, then review).
- Graph: `read -> extract_text` becomes a conditional edge `read -> first_attempt(effective_limits(...))` over `{extract_text, extract_vision}`; it emits one ROUTE trace (`ocr 0.52 < 0.65 · vision first`). Text inputs (`START -> extract_text`) are unaffected (no confidence).
- Supervisor: uses `effective_limits` (DRY with the edge); feedback is `[]` when the target is `extract_vision` and `vision_attempts == 0` (clean prompt after text attempts). Vision retries get the vision answer's own issues.

### 2.7 Settings

| Setting | Change | Default | Why |
|---|---|---|---|
| `PAPER_TRAIL_OCR__PSM` | default | 4 -> **6** | evidence 1 |
| `PAPER_TRAIL_PROMPTS__EXTRACTION_VERSION` | default | v1 -> **v2** | v1 kept for comparison |
| `PAPER_TRAIL_PROMPTS__VISION_VERSION` | new | **v1** | prompt `vision/v1` |
| `PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW` | new, 0..1 | **0.65** (0 = off) | between the measured good (0.79) and bad (0.41-0.49) reads in ADR-0010; recalibrate from the experiment trace |
| `PAPER_TRAIL_GRAPH__MAX_VISION_ATTEMPTS` | default | 1 -> **2** | vision-first gets one retry with its own issues |

No setting for the date window or tolerances.

### 2.8 Addendum findings

- **S2 (in scope, stretch, cut first): leading item codes read as sizes.** Pure `split_item_code(raw_text) -> (code | None, text)` in `domain/rules/normalization.py`: a leading run of 4-13 digits followed by whitespace (Costco 4-7 digit codes, PLU 4, UPC 11-13). The enrichment node sends `line: <text without code>` to the model; aliases still use the full `raw_text` (the code is a good alias key; no behaviour change in matching). v2 prompt also keeps codes out of `description`. Cost ~15 min.
- **Deferred: discount attributed to the item it reduces** (CHOC PISTACH 16.99 - 4.00 = 12.99 paid). Needs a new net-price column on `price_observations` (migration 0004, frozen-migration test), an attribution rule (Costco references the item code `1362394` -> needs S2's code split; Superstore discounts follow the item) and an owner decision on which price the history shows. It does not affect COMPLETED. Next plan; S2's `split_item_code` is its first building block.

## 3. Acceptance criteria

- **EA-AC-01** `parse_amount`: `"1.29"`->129, `"1,29"`->129, `"1,299.00"`->129900, `"$4.00"`->400, `"4.00-"`/`"-4.00"`/`"4.00 CR"`/`"(4.00)"`->-400, `"17.08 A"`->1708; `"1290"`, `"1.2"`, `""`, `"abc"` raise `PrintedValueError` naming the text.
- **EA-AC-02** `parse_quantity`: null/blank->1, `"2"`, `"0.460 kg"`->0.460, `"2 @"`->2; `"0"`/`"x"` raise. `parse_unit_price`: `"$3.31/kg"`->331, `"2/$10.00"`->500, `"3/$10.00"`->333, junk->None.
- **EA-AC-03** Sign/kind: an `item` printed `4.00-` becomes DISCOUNT -400; a `discount` printed `4.00` becomes -400; deposit/fee positive unchanged.
- **EA-AC-04** `PrintedExtraction.to_output()` on a Costco-shaped fixture (19 lines, six `4.00-` and `MULTIPLE 6.00-`, taxes summing to 14.03, total 203.19) yields an `ExtractionOutput` that passes `check_arithmetic`; tax = sum of `taxes`.
- **EA-AC-05** Several bad fields -> one `PrintedValueError` listing all, quoting printed text only; the node stores `extraction=None` and that `parse_error`; the next attempt's feedback is that message.
- **EA-AC-06** Dates: `26/11/02` with today 2026-11-08 -> 2026-11-02; `11/02/26` -> 2026-11-02; `02/11/26` -> 2026-02-11 (month first kept); `2026/11/05` -> 2026-11-05; `26/11/02 14:31` -> 2026-11-02; future-only readings -> None; existing date tests still pass.
- **EA-AC-07** Leak guard: an answer whose store or a line raw_text equals an example's is unparsed with the "copied the example" error; with `--no-examples` nothing is guarded.
- **EA-AC-08** Routing: `effective_limits` table (no photo / photo + confidence below / at threshold / None / threshold 0 / vision disabled). With OCR confidence 0.40 and a photo the graph runs `read -> extract_vision` (no `extract_text` LLM call), a ROUTE trace says vision first, and an always-invalid vision answer goes to review after `max_vision_attempts` with `text_attempts == 0`.
- **EA-AC-09** Feedback: after two failed text attempts, the first vision request has no `<validation_feedback>`; a second vision request carries only issues of the first vision answer.
- **EA-AC-10** Prompt files: `extraction/v2/schema.json` and `vision/v1/schema.json` equal the `PrintedExtraction` schema; every v2 example converts via `to_output()` and passes `check_arithmetic`; no v2 example names a real store (`SAMPLE` in every store name); `vision/v1` has no examples and its user message contains no OCR text; v1 files byte-identical.
- **EA-AC-11** Settings defaults per §2.7; `--prompt-version v1` still runs the v1 schema end to end (E1 comparison).
- **EA-AC-12** (S1) `share_amounts`: `[A amount null, B amount "10.00" qty "2"]` -> two lines qty 1, 500 each; odd split 10.01 -> 500/501; a null amount without a matching group quantity raises `no amount printed`.
- **EA-AC-13** (S2) `split_item_code("1281 1% MILK") == ("1281", "1% MILK")`, UPC 11-13 digits split, `"2% MILK"`/`"12 EGGS"`(<4 digits) unchanged; the enrichment request text has no leading code.
- **EA-AC-14** `make ci` green; graph snapshot updated only by the `read` conditional edge.

## 4. Files

| File | Change |
|---|---|
| `src/paper_trail/domain/rules/printed.py` | new: parsers, `PrintedValueError` (domain/errors), `line_kind`, S1 `share_amounts` |
| `src/paper_trail/domain/rules/dates.py` | two-digit-year recency rule, trailing time |
| `src/paper_trail/domain/rules/normalization.py` | S2 `split_item_code` |
| `src/paper_trail/agents/schemas.py` | `PrintedLine`, `PrintedExtraction`, `ExtractionAnswer`, `ANSWER_MODELS`, `ExtractionOutput.to_output` |
| `src/paper_trail/agents/prompts.py` | `RenderedPrompt.example_texts()` |
| `src/paper_trail/agents/nodes/extraction.py` | per-mode prompt + answer model, `to_output`, leak guard |
| `src/paper_trail/agents/routing.py` | `vision_first_below`, `effective_limits`, `first_attempt` |
| `src/paper_trail/agents/graph.py` | conditional edge after `read` (+ ROUTE trace) |
| `src/paper_trail/agents/nodes/supervisor.py` | `effective_limits`, clean first vision prompt |
| `src/paper_trail/agents/nodes/enrichment.py` | S2 code-free line text |
| `src/paper_trail/config.py` | §2.7 |
| `src/paper_trail/agents/prompts/extraction/v2/**`, `.../vision/v1/**` | new prompt files |
| `docs/adr/0017-llm-transcribes-code-computes.md` | new: printed-value schema, parsers, date rule, leak guard, vision-first, clean vision prompt; alternatives (prompt-only fixes, examples removed entirely for text) |
| ADR-0008 / 0009 / 0010 | amendments: first-attempt edge is supervisor policy in `routing.py`; amounts are strings in v2 schema; psm 6 + vision-first |
| `docs/PLAN.md` / `agent-core.md` | settings table rows only |

## 5. Tests for the test-author

- Unit `tests/unit/domain/test_printed.py` (EA-AC-01..03, 12), `test_dates.py` additions (EA-AC-06), `test_normalization.py` (EA-AC-13).
- Unit `tests/unit/agents/test_printed_extraction.py` (EA-AC-04, 05) with a Costco-shaped and a Superstore-shaped fixture in `tests/data/extraction/` (synthetic text, no real card numbers).
- Unit routing table (EA-AC-08 first part), prompt-file tests extended per version (EA-AC-10), config defaults (EA-AC-11; update the `psm=4` assertion in `test_config_agent_core.py`).
- Graph tests with `ScriptedLlm` + fake OCR confidence (EA-AC-07, 08, 09); assert on LLM request tags and messages, not on prose.
- Update the graph snapshot (EA-AC-14). No test touches a real Ollama.

## 6. Experiment (manual, after merge)

On the server, the two photos (Superstore A, Costco B) in the read-only receipts folder:

```
make process RECEIPTS=<dir> RUN_NAME=ea-v2 ARGS="--reprocess"
make process RECEIPTS=<dir> RUN_NAME=ea-v1 ARGS="--reprocess --prompt-version v1"   # control
```

Record per receipt from the trace: OCR mean confidence and rotation, route taken (text / vision first), attempts, prompt tags, final status, review reason. Success, from the DB of each run:

```sql
SELECT r.id, r.status, r.purchased_on,
       (SELECT count(*) FROM line_items l WHERE l.receipt_id = r.id) AS lines,
       (SELECT count(*) FROM price_observations p WHERE p.receipt_id = r.id) AS prices
FROM receipts r ORDER BY r.id;
```

- **Pass:** Costco `COMPLETED`, purchased_on 2026-11-05, lines >= 19, prices > 0. Superstore `COMPLETED` with purchased_on 2026-11-02 and prices > 0 is the stretch (needs S1); if it goes to review, the review reason must be the multi-buy line, not cents/sign/date.
- Report: v1 vs v2 outcome table (status, attempts, wall time), and the OCR confidences used to confirm or move `VISION_FIRST_BELOW`.
