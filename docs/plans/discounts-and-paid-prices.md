# Discounts and paid prices plan (a discount line -> the item it reduces)

Written 2026-11-08 by the planner. Status: **approved by the owner 2026-11-08, with the recommended answer to every decision (DA-1 to DA-6, section 15).** Built **first**, before `docs/plans/barcode-lookup.md` (which therefore moves to migration 0005). Builds on `docs/plans/extraction-accuracy.md` (S2 `split_item_code`, `is_item_reference`, `reconcile_multi_buy`), ADR-0006 (money as integer cents) and ADR-0017. It takes over the item "Deferred: discount attributed to the item it reduces" from extraction-accuracy §2.8 and from ADR-0017's Consequences. ADR-0017 gets an amendment; no new ADR (ADR-0018 stays with the barcode plan).

## Summary (approve from here)

1. **Problem.** On the owner's Costco receipts 2 and 4 every discount line is stored with `product_id` NULL, and price observations record the shelf price only. Gold Kiwi (`3139415 GOLD KIWI`, 15.99, position 15) is stored at 15.99 although 11.99 was paid: the discount `0000399102 / 3139415` -4.00 is printed right after it (position 16). Nothing ties a discount to the item it reduces. The JSON import sets `product_id` on a discount line only when the file names a product for that line, and it never applies the amount to an item's price. The README sentence saying discount lines "are linked to their product" is corrected.
2. **Owner decision (made 2026-11-08): store both prices; never overwrite the regular one.** Each price observation carries:
   - the regular unit price and the regular line total (the existing columns, meaning unchanged);
   - the discount, per line and per unit;
   - the paid amount;
   - an `on_sale` flag.

   The regular price is the price-history line; the paid amount is spending. A discount that cannot be tied to one item (`0000398549 / MULTIPLE` -6.00) stays at receipt level. It is never guessed or prorated; proration is noted as a possible later option.
3. **A pure domain rule, `attribute_discounts`** (new `domain/rules/discounts.py`).
   - It reads the referenced code from the discount line: `0000399102 / 3139415`, `TPD/1362394`, `0000391723 /1998068`. That is the token after the slash, or the token before it when the one after is not a code.
   - It compares that code with each ITEM line's code from `split_item_code`, ignoring leading zeros.
   - It pairs each discount with the **nearest preceding ITEM line with that code that has no discount yet**. Five CHOC PISTACH lines and five `/ 1362394` discounts make five pairs.
   - It refuses, keeping the discount at receipt level with a reason, for: `MULTIPLE`; no code; no earlier item without a discount; a discount larger than its item.
   - It never changes an amount.
4. **Plain code, no LLM.** The rule runs inside `RecordReceipt.assess`, so every path gets it: the graph's `save_receipt`, a review resolution, the JSON import and `--dry-run`. Price observations are computed from the stored links whenever they are written (`_record_prices`, `record_matches`).
5. **Schema: migration 0004** (the barcode plan moves to 0005).
   - `line_items.applies_to_position`: nullable; CHECK that it is set only on discount lines and never points at its own line.
   - `price_observations` is rebuilt with `discount_cents`, `unit_discount_cents`, `paid_cents` and `on_sale`, with CHECKs: `paid = line_total + discount`, `discount <= 0`, `paid >= 0`, `on_sale = (discount < 0)`.
   - Existing rows are copied with discount 0 and paid equal to the line total.
6. **Invariants.**
   - `paid_cents = line_total_cents + discount_cents`, exactly.
   - The regular line total equals the regular unit price x quantity within the existing 1-cent tolerance.
   - The receipt arithmetic is unchanged: the lines still sum to the subtotal, and a link adds no amount.
7. **Backfill command** for existing receipts: `paper-trail prices backfill [--run NAME] [--apply]`. It is a dry run by default, idempotent, and never changes a link already stored. Expected on the main database:
   - Gold Kiwi: regular 15.99, paid 11.99;
   - CHOC PISTACH: 16.99 to 12.99, five times;
   - `/1998068`: -5.00;
   - `/ 7635940`: -5.00, twice;
   - `MULTIPLE` -6.00: receipt level.
8. **Trace.** The `save_receipt` row reads "COMPLETED · 22 items · discounts 7 linked · 1 receipt-level". One NOTE per receipt names each refused discount and why.
9. **Superstore.**
   - `TPD/<code>` follows the same rule.
   - A discount with no code (`TPD` alone, `INSTANT SAVINGS`) stays at receipt level (owner decision DA-2).
   - A multi-buy (`2 @ 2/$10.00`) is a shared amount on the item lines, not a discount line, so no discount is recorded for it (DA-3).
10. **Also lands here** (needed by both plans): `split_item_code` accepts a leading `(N)` quantity marker (`(1)05700001285 CRV ...`).
11. **Estimate: about 6 h core**, plus 0.75 h that can be cut. ADR-0017 gets an amendment, which closes its "Deferred: discount attribution" item. No new ADR, so ADR-0018 stays with the barcode plan.

Owner decisions to confirm: DA-1 to DA-6 (section 15).

---

## 1. Evidence (owner's main database, Costco receipts 2 and 4)

| Item line | Discount line (right after, unless noted) | Today |
|---|---|---|
| pos 15 `3139415 GOLD KIWI` 1599 -> product 24 | pos 16 `0000399102 / 3139415` -400 | observation 1599; discount `product_id` NULL |
| `1362394 CHOC PISTACH` 1699 (x5 on one receipt) | `0000392208 / 1362394` -400 (x5) | five observations at 1699 |
| item 1998068 | `0000391723 /1998068` -500 (no space after the slash) | shelf price only |
| item 7635940 (x2) | `0000391089 / 7635940` -500 (x2) | shelf price only |
| none | `0000398549 / MULTIPLE` -600 | not attributable to one item |

How the code behaves today:
- Price observations are made for ITEM lines only: `application/receipts.py` `_observations`, and `application/matching.py` `_price` through `application/products.py` `price_observation`.
- The matching node skips DISCOUNT lines.
- The JSON import's `_linked` gives a line a `product_id` only when the file names a `product` on that line. That is how the README's "discount lines are linked to their product" is true for imports: a person can name one. It never changes any price.

Fixtures that already show the shapes:
- `tests/data/extraction/costco_printed.json`: 5 x (`1362394 CHOC PISTACH` 16.99, `TPD/1362394` -4.00), 2 x Gold Kiwi + `TPD/1543210`, and `MULTIPLE 6.00-`. The codes are synthetic.
- `tests/data/receipts/costco_simple.json`: `TPD/1234567` refers to the item at position 0, **three lines above**, not the adjacent one.

## 2. The rule (pure domain, `domain/rules/discounts.py`)

```python
class Refusal(StrEnum):
    MULTIPLE = auto()        # "... / MULTIPLE": a saving over several items
    NO_REFERENCE = auto()    # no code on the line ("TPD", "INSTANT SAVINGS")
    NO_ITEM = auto()         # no earlier ITEM line with that code that has no discount yet
    EXCEEDS_ITEM = auto()    # the discount is larger than the item's line total

@dataclass(frozen=True, slots=True)
class DiscountLink:
    discount_position: int
    item_position: int
    reference: str           # the code as printed on the discount line

@dataclass(frozen=True, slots=True)
class ReceiptLevelDiscount:
    discount_position: int
    reason: Refusal
    reference: str | None

@dataclass(frozen=True, slots=True)
class DiscountAttribution:
    links: tuple[DiscountLink, ...]
    receipt_level: tuple[ReceiptLevelDiscount, ...]

def discount_reference(raw_text: str) -> str | Literal["MULTIPLE"] | None
def attribute_discounts(lines: Sequence[LineItem]) -> DiscountAttribution
def with_attribution(lines: Sequence[LineItem], found: DiscountAttribution) -> tuple[LineItem, ...]
```

**Reading the reference (`discount_reference`).** The pattern is `^\s*(?P<left>[A-Za-z0-9]+)\s*/\s*(?P<right>[A-Za-z0-9]+)`, and anything after it is ignored.
- `right` is `MULTIPLE` (any case) -> `"MULTIPLE"`.
- `right` is 4-13 digits -> `right`. This covers `0000399102 / 3139415`, `TPD/1362394` and `0000391723 /1998068`.
- Otherwise `left` is 4-13 digits -> `left`. This covers `1362394/TPD`, which `is_item_reference` already treats as a reference.
- Otherwise None.

Costco's left-hand number (`0000399102`) is the coupon number. It is never compared when a code stands on the right.

**Code equality.** Both sides go through `lstrip("0")`: the item's code from `split_item_code(raw_text)` and the reference. So `0001281` equals `1281`, and an 11-digit UPC equals the same digits printed with a leading zero.

**Pairing (`attribute_discounts`).** Discount lines are handled in position order, considering only DISCOUNT lines whose `applies_to_position` is None. Lines a person already attributed are kept, and their items count as taken.
1. Reference None -> `NO_REFERENCE`; `MULTIPLE` -> `MULTIPLE`.
2. Candidates are the ITEM lines at a smaller position whose code equals the reference and that are not yet taken.
3. No candidate -> `NO_ITEM`. Items that come *after* the discount are never used: Costco and Superstore print the saving under its item.
4. The candidate with the largest position (nearest above) is chosen. With 5 CHOC PISTACH lines interleaved with 5 discounts, each discount takes the line just above it. With all 5 items printed first and all 5 discounts after, the first discount takes the 5th item, the second the 4th, and so on. Every item still gets exactly one discount, and the per-item result is the same because the lines are identical.
5. `abs(discount) > item.line_total` -> `EXCEEDS_ITEM`, so paid can never be negative.
6. Otherwise the pair is made and the item is taken.

**Exact or nothing.** Every refusal leaves the discount at receipt level, with its reason in the result (it goes into the trace). The rule never changes an amount, a kind or a quantity. `with_attribution` returns the lines with `applies_to_position` set on linked discounts, and nothing else changed.

**Fallback rules considered and refused** (each would be a guess):
- the line directly above, for a discount without a code (owner decision DA-2);
- a following item;
- splitting one discount over several identical lines;
- prorating MULTIPLE over the receipt.

## 3. Quantities above 1 and weighed items

A linked discount D (negative cents) on an item with quantity q, line total L and unit price U (as printed, else `L / q`):

| Field | Value | Example (2 x 16.99, discount 8.00) |
|---|---|---|
| `unit_price_cents` (regular) | U | 1699 |
| `line_total_cents` (regular) | L | 3398 |
| `discount_cents` (line) | D | -800 |
| `unit_discount_cents` | `Money(D).divided_by(q)`, half-up like the unit price | -400 |
| `paid_cents` | L + D, exact | 2598 |
| `paid_unit_price` (computed, not stored) | `Money(L + D).divided_by(q)` | 1299 |
| `on_sale` | D < 0 | true |

- The per-line values are the exact ones. Per-unit values are rounded for display and charts.
- Weighed lines (q = 0.460 kg) work the same way: the per-unit values are per kg.
- A discount of 4.00 on a quantity-3 line gives -133 per unit; the per-line -400 stays exact.

## 4. Superstore TPD and multi-buy

- **`TPD/1234567`:** same rule. The code after the slash is matched against the item's code, including 11-digit UPCs and codes with a `(1)` marker (see section 8, `split_item_code`).
- **A discount with no code** (`TPD`, `INSTANT SAVINGS`, points redemptions): receipt level (`NO_REFERENCE`). DA-2 asks whether one store should attach to the line directly above.
- **Multi-buy** (`2 @ 2/$10.00  10.00` over two products) is not a discount line. `share_amounts` and `reconcile_multi_buy` (ADR-0017) set the two item lines' totals to the shared amounts before saving. Attribution runs later, on the final lines, and finds no discount line for them.
  - Result: regular = paid = the shared amount, `on_sale` false (DA-3).
  - The shelf price printed in the promo text (`$5.99 ea or 2/$10.00`) is not read: unit prices from promo text are what ADR-0017 stopped trusting.
  - A later option is to carry a "deal" marker from extraction.
- **Interplay:** attribution never changes amounts, so `reconcile_multi_buy` (which runs in the extraction node, before save) and `check_arithmetic` see the same numbers as today. A test pins that the arithmetic report is identical with and without attribution.

## 5. Where linking happens in the graph

- **`RecordReceipt.assess(draft)`** (application) calls `attribute_discounts` on the masked draft's lines and returns `with_attribution(...)` in `ReceiptAssessment.draft`, plus `ReceiptAssessment.discounts: DiscountAttribution`.
- `__call__`, `replace` and `--dry-run` all go through `assess`, so the same deterministic code covers:
  - the graph (`persist` -> `save_receipt` -> `RecordReceipt.replace`);
  - a review resolution;
  - the JSON import.
- No node, prompt or model output changes. The extraction schemas (`ExtractedLine` for v1, `PrintedLine` for v2 and vision) do **not** gain the field, so a model can never set a link.
- The graph draft carries a link only when a person put one there (a review file or an import file), through `DraftLinePayload.applies_to_position`. It is optional, and old checkpoints read as None.
- **Price observations** are computed by `price_observation(receipt, item, purchased_on)` from the receipt's stored lines: the discount whose `applies_to_position == item.position`, else none. The two writers that already call it need no new logic:
  - `_record_prices` (import, `replace` without matching);
  - `record_matches._price` (graph, after linking products).
- `items correct` moves the observation with `reassign`, which carries the discount fields along.
- A discount line's `product_id` stays NULL on the agent path (DA-4). The product of a discount is the product of its item.

## 6. Schema: migration `0004_paid_prices.py`

```sql
-- line_items: ADD COLUMN (no rebuild: keeps the FTS triggers and the incoming foreign keys)
ALTER TABLE line_items ADD COLUMN applies_to_position INTEGER
  CHECK (applies_to_position IS NULL
         OR (kind = 'discount' AND applies_to_position >= 0 AND applies_to_position <> position));

-- price_observations: rebuilt (nothing references it; it has no triggers). Copies every row with
-- discount 0, paid = line total, on_sale 0; recreates ux_price_observations_line_item,
-- ix_price_observations_product_date and the per-FK indexes exactly as 0002 named them.
CREATE TABLE price_observations_new (
  ... the 0002 columns ...,
  discount_cents      INTEGER NOT NULL DEFAULT 0 CHECK (discount_cents <= 0),
  unit_discount_cents INTEGER NOT NULL DEFAULT 0 CHECK (unit_discount_cents <= 0),
  paid_cents          INTEGER NOT NULL CHECK (paid_cents >= 0),
  on_sale             INTEGER NOT NULL DEFAULT 0 CHECK (on_sale IN (0, 1)),
  CHECK (paid_cents = line_total_cents + discount_cents),
  CHECK (on_sale = (discount_cents < 0))
);
```

- **Why a position and not `applies_to_line_item_id`.** `RecordReceipt.replace` deletes and re-inserts a receipt's lines, so line ids change on every save. A draft (import, review file, checkpoint) has no ids at all. Positions are stable and already unique per receipt (`ux_line_items_receipt_position`).
- There is no foreign key: a composite foreign key cannot be added with ADD COLUMN, and a `line_items` rebuild would touch the FTS triggers. The rest is checked in the domain (section 7): the target exists, is an ITEM line, and has at most one discount.
- `unit_discount_cents` is stored for SQL convenience. A mapper test pins it to `Money(discount).divided_by(quantity)`. There is no CHECK on it, because SQL rounding would duplicate the Decimal rule.
- **Downgrade:** rebuild `price_observations` without the four columns, and `ALTER TABLE line_items DROP COLUMN applies_to_position` (0003's downgrade already proves DROP COLUMN works).
- Run as one Alembic migration in its own transaction, `PRAGMA foreign_keys` unchanged; nothing references `price_observations`.
- `adapters/sqlite/schema.py` mirrors the new columns. `test_schema_matches_metadata` passes, and the frozen migrations 0001-0003 are untouched.
- **Coordination:** `docs/plans/barcode-lookup.md` renumbers its migration to `0005_product_codes.py` (`down_revision = "0004"`).

## 7. Domain model changes (`domain/models.py`)

```python
@dataclass(frozen=True, slots=True)
class LineItem:
    ...
    applies_to_position: int | None = None   # DISCOUNT only: the ITEM line it reduces
    # __post_init__: set only on DISCOUNT lines, never == position, >= 0

class Receipt / ReceiptDraft:
    # __post_init__ (new check, DomainValidationError naming both positions):
    #   every applies_to_position names an ITEM line of this receipt; no ITEM line is named twice

@dataclass(frozen=True, slots=True)
class PriceObservation:
    ...                      # unchanged fields; unit_price and line_total are the REGULAR prices
    discount: Money          # line-level, <= 0, same currency
    def __post_init__(self): # currency check (existing) + discount <= 0 + paid >= 0
    @property paid -> Money                  # line_total + discount
    @property unit_discount -> Money         # discount.divided_by(quantity)
    @property paid_unit_price -> Money       # paid.divided_by(quantity)
    @property on_sale -> bool                # discount < 0
```

`discount` is a required field, so every constructor states it: `price_observation` and `price_from_row` (the mapper). Making it required means no observation is ever silently created without one.

## 8. Application and adapter changes

- **`application/products.py` `price_observation(receipt, item, purchased_on)`:** finds the discount line with `applies_to_position == item.position`. Its `line_total` becomes `discount`; with none, `Money(0, currency)`.
- **`application/receipts.py`:**
  - `assess` applies the rule (section 5) and `ReceiptAssessment` gains `discounts`.
  - `RecordReceiptResult` gains `discounts`.
  - `_record_prices` is unchanged; it calls the new `price_observation`.
- **`adapters/sqlite/mappers.py` / `receipt_repository.py`:** `applies_to_position` in the line values and rows, and the four observation columns.
- **`application/payloads.py` `DraftLinePayload`:** optional `applies_to_position`, both directions.
- **`interfaces/cli/schemas.py` `LineImport`:** optional `applies_to` (0-based position of the item line). A person's explicit link is validated and kept; the rule fills only the rest. Cuttable.
- **`tools/models.py` `SaveReceiptOutput`:** gains `discounts_linked: int` and `discounts_receipt_level: list[{position, raw_text, reason}]`. `agents/tracing.py` `_save_summary` appends "· discounts N linked · M receipt-level" when there are any.
- **`interfaces/web/schemas/receipts.py` `LineItemOut`:** gains `applies_to_position`. The OpenAPI spec and TS client are regenerated. Cuttable.
- **`domain/rules/normalization.py` `split_item_code`:** accepts a leading `(N)` marker (N 1-2 digits): `"(1)05700001285 CRV CHKKN PBLN BR MRJ"` -> `("05700001285", "CRV CHKKN PBLN BR MRJ")`. The barcode plan reuses it.

## 9. Backfill: `paper-trail prices backfill [--run NAME] [--apply] [--receipt ID]`

- For each COMPLETED receipt with discount lines, newest first, the attribution is computed on the **stored** lines. Links already stored are kept: the rule fills only the discount lines with no link.
- **The dry run (default)** prints one row per discount line: receipt, position, raw text, amount, then either "-> item pos N `<raw text>`: regular 15.99, paid 11.99" or "receipt level (multiple | no_reference | no_item | exceeds_item)". A totals line follows.
- **`--apply`**, one unit of work per receipt:
  - set `applies_to_position` on the linked discount lines (`ReceiptRepository.set_discount_links(receipt_id, {discount_position: item_position})`, which refuses to overwrite a different existing link);
  - update each affected observation's discount fields (`PriceRepository.set_discount(line_item_id, discount: Money)`, which recomputes the derived columns from the domain object);
  - receipts without a purchase date have no observations, so only their links are set.
- **Idempotent:** a second `--apply` reports "0 changes". It writes no amounts, no products and no aliases, and calls no model.
- `--run NAME` targets a run database, as the other commands do.
- **Expected on the main database** (from the owner's listing): every coded discount linked, MULTIPLE at receipt level. Gold Kiwi product 24: regular 15.99, discount -4.00, paid 11.99. The exact counts are whatever the dry run prints; they go into the experiment notes.

## 10. Trace

- `save_receipt` TOOL row: "COMPLETED · 22 items · discounts 7 linked · 1 receipt-level". Its data carries the full `discounts_receipt_level` list.
- The `persist` node emits one `NOTE` (agent SUPERVISOR, name `discounts`) **only when** something stays at receipt level, for example "discount pos 4 `0000398549 / MULTIPLE` -6.00 kept at receipt level: multiple". No discount stays unexplained.
- No new `TraceKind`; NOTE exists since ADR-0017.

## 11. README "What gets saved"

- Correct the Gold Kiwi example. Each `price_history` entry shows `"unit_price_cents": 1599, "line_total_cents": 1599, "discount_cents": -400, "unit_discount_cents": -400, "paid_cents": 1199, "on_sale": true` where a discount was printed, with one line explaining: "regular 15.99 is the price history; 11.99 is what was paid".
- Add `applies_to_position` to the `line_items` bullet. Add the regular/discount/paid/on_sale wording to the `price_observations` bullet, and add "discounts that cannot be tied to one item (`MULTIPLE`) stay on the receipt".
- Update the SQL example to select `paid_cents` and `on_sale`.
- Spending query note: paid per product plus receipt-level discounts equals what was spent on items.
- Fix line 241 of the import section: "deposit, discount and fee lines get a product only when the file names one; a discount that names an item's code (`TPD/1234567`) is applied to that item's paid price".

## 12. Acceptance criteria

- **DP-AC-01** `discount_reference`, table-driven:
  - `0000399102 / 3139415` -> `3139415`; `TPD/1362394` -> `1362394`; `0000391723 /1998068` -> `1998068`; `1362394/TPD` -> `1362394`;
  - `0000398549 / MULTIPLE` and `.../multiple` -> `MULTIPLE`;
  - `TPD`, `INSTANT SAVINGS`, `2 @ 2/$10.00` -> None.
- **DP-AC-02** Pairing:
  - interleaved 5 x (item, discount) -> each discount takes the line above;
  - 5 items then 5 discounts -> 5 pairs, every item taken once;
  - `costco_simple.json` -> `TPD/1234567` takes position 0 across 3 lines;
  - leading zeros ignored;
  - `(1)05700001285 ...` matched by `TPD/05700001285`.
- **DP-AC-03** Refusals:
  - MULTIPLE -> `MULTIPLE`; no code -> `NO_REFERENCE`;
  - a code with no item above, or only taken items -> `NO_ITEM`;
  - an item printed after the discount is never used;
  - a discount larger than its item -> `EXCEEDS_ITEM`;
  - a person's existing link is kept and its item counts as taken.
- **DP-AC-04** The rule changes no amount, kind or quantity. For every fixture the `check_arithmetic` report is identical with and without attribution (pinned on `costco_printed.json` and the Superstore-shaped fixture, including a `reconcile_multi_buy` case).
- **DP-AC-05** Domain invariants:
  - `LineItem.applies_to_position` is refused on non-DISCOUNT lines and on itself;
  - a receipt naming a missing or non-ITEM line, or one item twice -> `DomainValidationError` naming both positions;
  - `PriceObservation` refuses a positive discount, a negative paid, and a currency mismatch;
  - properties: `paid`, `unit_discount` (half-up), `paid_unit_price`, `on_sale`.
- **DP-AC-06** `price_observation` for Gold Kiwi (1599, discount -400) -> paid 1199, unit discount -400, on_sale. For 2 x 16.99 with -8.00 -> section 3's row. No discount -> discount 0, paid = line total, not on sale.
- **DP-AC-07** Migration 0004:
  - upgrade and downgrade round-trip;
  - existing observations are copied with discount 0, paid = line total, on_sale 0;
  - every 0002 index on `price_observations` is present after the rebuild;
  - the CHECKs refuse `paid != line_total + discount`, a positive discount, a negative paid, an inconsistent on_sale, and `applies_to_position` on an item line or on itself;
  - the `line_items` FTS triggers still fire;
  - `test_schema_matches_metadata` passes; 0001-0003 are byte-identical.
- **DP-AC-08** Repositories:
  - `applies_to_position` and the four columns round-trip;
  - `reassign` keeps the discount fields;
  - `set_discount_links` refuses to overwrite a different link;
  - `set_discount` writes derived columns consistent with the domain object.
- **DP-AC-09** `RecordReceipt`:
  - importing `costco_simple.json` stores `applies_to_position = 0` on the TPD line and an observation with paid = 899 - 500 = 399;
  - `--dry-run` shows the link and writes nothing;
  - an import file with `applies_to` set by hand is kept; an invalid one exits 2 naming the field.
- **DP-AC-10** Graph, with a scripted LLM and fake embeddings, on the Costco fixture end to end:
  - after `save_matches` the 5 CHOC PISTACH observations have paid 1299 and the Gold Kiwi ones 1199;
  - MULTIPLE has no link;
  - the `save_receipt` trace summary counts them;
  - exactly one NOTE names MULTIPLE;
  - no LLM request contains `applies_to`;
  - a resumed run (crash after `save_matches`) gives the same observations.
- **DP-AC-11** `items correct` on a discounted line moves the observation to the new product with discount and paid unchanged.
- **DP-AC-12** Backfill:
  - the dry run writes nothing and lists every discount with its outcome;
  - `--apply` sets links and observation fields;
  - a second `--apply` -> 0 changes;
  - an existing link is never changed;
  - a receipt without a date gets links only;
  - `--run` targets that run's database.
- **DP-AC-13** `split_item_code("(1)05700001285 CRV ...") == ("05700001285", "CRV ...")`; every existing ADR-0017 case is unchanged.
- **DP-AC-14** `make ci` green, coverage of at least 90 %. The OpenAPI spec and TS client are regenerated (`LineItemOut.applies_to_position`). The graph snapshot is unchanged (no new node). README and ADR-0017 amendment written.

## 13. Test map

No real Ollama, no network; codes in tests are the synthetic fixture codes.

| Suite | File | ACs |
|---|---|---|
| unit/domain | `tests/unit/domain/rules/test_discounts.py` (table-driven), `test_normalization.py` additions, `test_models_price_observation.py` | 01-06, 13 |
| unit/application | `test_record_receipt.py` additions (import, dry run, explicit link), `test_price_observation.py` | 06, 09 |
| integration | `test_migration_0004.py`, `test_price_repository_sqlite.py` additions, `test_prices_backfill_sqlite.py` | 07, 08, 12 |
| unit/agents (graph via `agent_harness`, ScriptedLlm) | `test_discount_attribution_graph.py` | 10 |
| unit/application | `test_corrections.py` addition | 11 |
| unit/interfaces | `test_cli_prices_backfill.py`, `test_cli_import_applies_to.py` | 09, 12 |
| e2e | `tests/e2e/test_receipts_api.py`: `applies_to_position` in `GET /api/receipts/{id}` | 14 |

Pin these in `docs/tests/discounts-and-paid-prices.md`, as the other plans do.

## 14. Files

| File | Change |
|---|---|
| `src/paper_trail/domain/rules/discounts.py` | new (section 2) |
| `src/paper_trail/domain/rules/normalization.py` | `(N)` marker in `split_item_code` |
| `src/paper_trail/domain/models.py` | `LineItem.applies_to_position`, Receipt/Draft link check, `PriceObservation.discount` + properties |
| `migrations/versions/0004_paid_prices.py` | section 6 |
| `src/paper_trail/adapters/sqlite/{schema,mappers,receipt_repository,price_repository}.py` | columns, `set_discount_links`, `set_discount` |
| `src/paper_trail/ports/repositories.py` | the two new repository methods |
| `src/paper_trail/application/{products,receipts}.py`, `application/prices.py` (new: backfill) | sections 5, 8, 9 |
| `src/paper_trail/application/payloads.py`, `interfaces/cli/schemas.py` | optional link fields |
| `src/paper_trail/tools/models.py`, `tools/handlers.py`, `agents/tracing.py`, `agents/nodes/persist.py` | output counts, summary, NOTE |
| `src/paper_trail/interfaces/cli/{main,prices}.py` | `prices backfill` |
| `src/paper_trail/interfaces/web/schemas/receipts.py`, OpenAPI spec, TS client | `applies_to_position` |
| `README.md`, `docs/adr/0017-...md` (amendment), `docs/tests/discounts-and-paid-prices.md` | |

## 15. Owner decisions

**Approved 2026-11-08: the recommended option for each.**

- **DA-1** Columns: keep `unit_price_cents`/`line_total_cents` as the regular price and add `discount_cents`, `unit_discount_cents`, `paid_cents`, `on_sale` (recommended). The alternative, renaming to `regular_*`, breaks the README queries and the API for no gain.
- **DA-2** A discount printed without a code (`TPD`, `INSTANT SAVINGS`): receipt level (recommended), or attached to the ITEM line directly above. If wanted, the latter would be a per-store switch, defined only after a real Superstore receipt shows the pattern.
- **DA-3** Multi-buy: no discount recorded, regular = paid = shared amount, `on_sale` false (recommended; documented as a limit). The alternative is `on_sale` true with an unknown regular price.
- **DA-4** A discount line's `product_id` stays NULL; its product follows from the item it applies to (recommended). The alternative copies the item's product, which must then be kept in step on every correction.
- **DA-5** Two discounts naming one single item line: the second is refused (`NO_ITEM`, recommended). The alternative is adding them together.
- **DA-6** Confirm: MULTIPLE is never prorated in this plan.

## 16. Estimate and cut line

| Piece | Estimate |
|---|---|
| `discounts.py` rule + `(N)` marker + table tests | 1.25 h |
| Domain model invariants and `PriceObservation` | 0.5 h |
| Migration 0004, schema mirror, mappers, repositories | 1.25 h |
| `assess` wiring, `price_observation`, payload field, tool output, trace summary | 1.25 h |
| `prices backfill` | 1 h |
| README, ADR-0017 amendment, test doc | 0.5 h |
| **Core** | **about 6 h** (agent-pipeline estimate, including review) |
| Cuttable: import `applies_to` 0.25 h, web schema + OpenAPI regen 0.25 h, per-receipt NOTE 0.25 h (the summary counts stay) | 0.75 h |

**Cut order:** web schema exposure, then the import `applies_to` field, then the NOTE.

**Minimum:** the rule, migration 0004, the observation fields, the wiring in `assess`, the backfill.

## 17. Later options (not in this plan)

- Proration of `MULTIPLE` over the receipt's items: a separate, labelled value, never mixed into `paid_cents`.
- Reading the regular price of a multi-buy from the promo text.
- An `items discount RECEIPT POS --to ITEM_POS | --receipt-level` correction command. Today a person fixes a link with a review or import file.
- The item page chart: regular line plus paid points marked "on sale".
