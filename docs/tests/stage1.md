# Stage 1 test plan: acceptance criteria to tests

Written by the test author on 2026-11-06, before any Stage 1 implementation exists. Every test listed here fails until Stage 1 is built (most at collection, with `ModuleNotFoundError`). Source of truth: `docs/PLAN.md` §3 and `docs/ROADMAP.md`. The suites, markers and Make targets are the Stage 0 ones (`docs/tests/stage0.md`).

## Suites and how they run

| Suite | Stage 1 location | Runs with |
|---|---|---|
| Unit (fakes only) | `tests/unit/domain/`, `tests/unit/application/`, `tests/unit/interfaces/` | `make test`, `make test-unit` |
| Contract (fake and SQLite) | `tests/contract/test_*_repository.py`, `test_job_queue.py`, `test_review_queue.py`, `test_vector_index.py`, `test_text_search_index.py`, `test_unit_of_work.py` | `make test`, `make test-unit` |
| Integration (temp SQLite file) | `tests/integration/test_migration_0002.py`, `test_schema_matches_metadata.py`, `test_sqlite_types.py`, `test_fts.py`, `test_cascades.py`, `test_job_queue_concurrency.py`, `test_record_receipt_masking.py`, `test_database_health_probe.py` | `make test`, `make test-integration` |
| End to end | `tests/e2e/test_receipts_api.py`, `tests/e2e/test_import_and_query.py` | `make test` |
| Repository checks | `tests/repo/test_migrations_frozen.py` | `make test` |
| Frontend | `frontend/src/features/receipts/*.test.ts(x)`, `frontend/tests/tooling/{generated-api,money-formatting}.test.ts` | `make fe-test` |

Every contract test takes the `uow_factory` fixture from `tests/contract/conftest.py`. It is parametrized `fake` / `sqlite`, so each test runs once against `tests.fakes.unit_of_work.FakeUnitOfWork` and once against `SqliteUnitOfWork` on a freshly migrated file. Ports are always reached through a unit of work, as the application reaches them. Both run on the same `FakeClock`.

### Test support written by the test author (not by the implementer)

- `tests/factories.py`: builders for every domain object. The defaults form one consistent receipt (eggs 8.99 + milk 2 x 3.29 = 15.57, tax 0.78, total 16.35). It also defines `TEST_PAN` (the Visa test number `4111 1111 1111 1111`, never a real card).
- `tests/fakes/state.py` (`FakeState`, `newest_first_page`), `tests/fakes/repositories.py`, `tests/fakes/queues.py`, `tests/fakes/search.py`, `tests/fakes/unit_of_work.py` (`FakeDatabase`, `FakeUnitOfWork`, `fake_uow_factory`): an in-memory port for every Stage 1 port. A unit works on a deep copy of the committed state and publishes it on `commit()`, which gives the fake the same all-or-nothing behaviour as a transaction. The fake vector index is brute-force cosine distance. The fake text index uses the same tokenising rule as the adapter must.
- `tests/storage.py`: `in_unit`, `sqlite_uow_factory(engine, clock)`, `seed_product_and_receipt` and `table_values` (every cell of every table in a database file, FTS shadow tables included).
- `tests/pans.py`: an independent Luhn oracle (`luhn_ok`, `with_check_digit`, `card_numbers`). A test oracle must not share the code it checks.
- `tests/data/receipts/costco_simple.json`: the sample import (5 lines with a weighed item and a discount; 32.40 + 0.52 = 32.92).
- `tests/support.py`: `SCHEMA_HEAD = "0002"`, `require_id`.
- Frontend: `src/test/fixtures/receipts.ts` (typed from the generated schema), `src/test/intersection-observer.ts` (a controllable `IntersectionObserver`; `intersectAll()` stands for "the user scrolled to the end"), installed in `src/test/setup.ts`. `src/test/msw/handlers.ts` gains a default `GET /api/receipts` that returns an empty page.

### Stage 0 tests that change meaning in Stage 1

- `SCHEMA_HEAD` is `"0002"`, so the Stage 0 migration, CLI and health tests expect the new head.
- `tests/e2e/test_openapi_contract.py::test_s0_ac_32_committed_spec_matches_the_backend` fails until `make api-client` regenerates and commits `frontend/openapi.json` and `src/api/schema.d.ts` with the Stage 1 routes. The frontend tests and fixtures are typed from those generated schemas.
- `frontend/src/routes/pages.test.tsx` still requires `/receipts` to show an empty state with a heading and a description. With the default empty-page handler, the receipt list's empty state satisfies it.

## Details the plan left open, pinned by the tests

Where the plan is silent, the tests take the smallest reasonable reading. The implementer builds to these.

### Domain

- `Money.amount_cents` must be a real `int`. A `bool`, `float`, `Decimal` or `str` raises `InvalidMoneyError`. `InvalidMoneyError`, `CurrencyMismatchError` and `InvalidQuantityError` subclass `DomainValidationError`, so they surface as 422. `sum_money(values, currency)` raises `CurrencyMismatchError` when any value is in another currency. ROUND_HALF_UP means half away from zero (`-0.5` cents rounds to `-1`).
- `ReceiptDraft` validates `payment_last4` against ASCII `^[0-9]{4}$` (Arabic-Indic digits are rejected) and raises `DomainValidationError`. `LineItem` raises `InvalidQuantityError` for `quantity <= 0`.
- `IssueCode` is a `StrEnum` in `domain/rules/arithmetic.py`, and `HitKind` is a `StrEnum` in `ports/search.py`. Like every enum, their value is the lowercase name, also through `json.dumps`.
- Cursor: URL-safe base64 of `"<created_at ISO-8601>|<id>"`. `encode_cursor` converts the time to UTC, and `decode_cursor` returns an aware UTC datetime. The following raise `DomainValidationError`: invalid base64 or UTF-8, a missing `|`, a naive or unparsable time, and a non-integer or negative id. `Page` is a frozen generic dataclass.
- `check_arithmetic`:
  - `expected` is the amount computed from other fields, and `actual` is the amount printed on the receipt.
  - Messages number lines from 1 (`position` 2 is "line 3"). They print amounts as plain two-decimal numbers (`9.98`, `-1.00`).
  - Rule 3 with no tax counts the tax as zero, as rule 4 does.
  - When any amount is in a currency other than the receipt's, only `CURRENCY_MISMATCH` issues are reported: one per foreign amount, with `position` set for lines and `None` for receipt-level amounts, and `actual` the foreign amount. The sum rules are skipped and nothing raises.
  - Every independent problem is reported in one pass.
  - `EMPTY_RECEIPT` has `expected`, `actual` and `position` all `None`. `NEGATIVE_ITEM` carries the line's position and its negative total as `actual`.
  - Lines without a unit price skip rule 1.
  - A stored `Receipt` is a valid `ReceiptLike`.
- `normalize_alias`:
  - Steps: NFKC, casefold, collapse whitespace (tabs and no-break spaces included), strip.
  - Removals: a leading item code `^\d{4,}\s`; trailing tax-flag tokens `h`, `gst`, `hst`, `pst`, `qst`; trailing price tokens `\$?\d+\.\d{2}-?`.
  - The removals repeat until nothing changes, which makes the function idempotent.
  - Size tokens, `%`, accents and inner digits are kept (`"KS 2% MILK 4L"` becomes `"ks 2% milk 4l"`).
- `normalize_store_name`:
  - Steps: NFKC, casefold, collapse whitespace, strip.
  - Store numbers dropped: `#123`, `store 123`, a bare trailing number.
  - Suffix tokens dropped: `inc`, `ltd`, `llc`, `corp`, `co`, `wholesale`, `supercentre`, `supercenter`. A trailing `.` or `,` on them is ignored (`"Costco Wholesale Corp."` becomes `"costco"`).
  - The function is idempotent.
- `unit_price_per_base_unit`:
  - The amount bought is `quantity x size_value` in `size_unit`. Conversions: g/1000 to kg, ml/1000 to l, lb = 0.45359237 kg, oz = lb / 16, EACH to each.
  - `cents_per_base` is an exact `Decimal` (no rounding, no float).
  - The function returns `None` whenever `size_value` or `size_unit` is `None`.

### Storage adapters and fakes

- `SqliteUnitOfWork(engine, clock)` lives in `adapters/sqlite/unit_of_work.py`. A unit reads its own uncommitted writes. Leaving without `commit()`, or with an exception (which propagates), rolls back every repository's writes.
- `adapters/sqlite/schema.py` exports `metadata` holding exactly the eight regular tables. `adapters/sqlite/types.py` exports `UtcDateTime()`, `IsoDate()`, `DecimalText()` and `StrEnumType(EnumClass)`:
  - `UtcDateTime` writes fixed-width `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, converting other offsets to UTC, so text order is time order. A naive datetime fails at bind time (`StatementError`).
  - `DecimalText` stores `str(Decimal)` as TEXT and refuses floats.
  - `StrEnumType` stores `.value` and loads the member.
- Receipts:
  - `add` keeps the given `created_at`/`updated_at`, assigns ids to the receipt and every item, and returns items ordered by `position` (also on `get`).
  - `update_status` and `replace_contents` stamp `updated_at = clock.now()`. `replace_contents` keeps the stored `created_at` and replaces all items, which get new ids. Both raise `NotFoundError` for an unknown id.
  - A garbage cursor in `list` raises `DomainValidationError`.
- Products:
  - Tags round-trip as an ordered tuple (a JSON array in the column).
  - `find_by_alias` matches the stored alias text exactly, since callers pass `normalize_alias` output. `store_key=None` matches only global aliases. A store key tries the store's alias, then the global one.
  - `add_alias` on an existing `(alias, store_key)` key keeps a `USER_CORRECTION` row unchanged (and returns it) when the incoming source is any other. Every other combination replaces the row in place: same id, new `product_id`, `source` and `created_at`.
  - `aliases()` is ordered by id.
- Prices: one observation per line item. A duplicate, also within one batch, raises `ConflictError` (the adapter translates the UNIQUE violation). An empty batch returns `()`.
- Corrections: `latest` matches `kind`, `subject` and `store_key` exactly (`None` only matches `None`). It returns the newest by `created_at`, and the higher id wins a tie.
- Jobs:
  - `enqueue` stamps `available_at = created_at = updated_at = now`.
  - `claim` picks, by earliest `available_at` and then lowest id, among PENDING jobs with `available_at <= now` and RUNNING jobs with `lease_expires_at < now` (strictly). It sets RUNNING, `locked_by`, `lease_expires_at = now + lease` and `updated_at`, and does not count an attempt.
  - The owner of a job is the worker holding it RUNNING with `locked_by` equal to its id. An expired lease is still owned until someone else claims it. Any other caller gets `ConflictError`, and the row is unchanged. An unknown id raises `NotFoundError`.
  - `complete`, `fail` and `release` clear `locked_by` and `lease_expires_at`. `fail(retry_at=None)` is final at once. `pending_count` counts every PENDING job, scheduled ones included.
- Reviews:
  - `open` stamps `created_at`.
  - `resolve` and `dismiss` both set `resolved_at = now`. Closing a non-OPEN item raises `ConflictError`, and an unknown id raises `NotFoundError`.
  - A closed receipt can get a new OPEN item.
  - `list_open` pages newest first with the receipts' cursor scheme.
- Vectors: `dimension == 768`. A wrong length in `upsert` or in `nearest` raises `DomainValidationError`. Deleting an unknown id is a no-op. Equal distances are ordered by ascending product id, and an opposite vector is at distance 2.0.
- Text search:
  - The query is split into tokens, each a maximal run of Unicode letters and digits (`[^\W_]+`), so `"free-run"` is two tokens and `"24CT"` one. Each token becomes a quoted prefix term (`"tok"*`), and all terms must match.
  - A query without tokens returns `()`. Operator words are ordinary prefixes: `"OR"` finds `"ORG"`.
  - Case and diacritics are ignored on both sides.
  - Line-item and product hits come back in one tuple sorted by descending score, cut to `limit`. In SQLite the score is the negated `bm25()`, so it is positive.
- Migration `migrations/versions/0002_core_schema.py`:
  - The file declares `revision = "0002"` and `down_revision = "0001"`, and imports nothing from `paper_trail`.
  - Index names are free. Tests identify indexes by key columns, uniqueness and partial clause.
  - Exactly three triggers on `line_items` (FTS insert, update and delete) and four on `products` (FTS insert, update and delete, plus deleting the `product_vectors` row).
  - Downgrade to `0001` leaves only `alembic_version` and SQLite's own `sqlite_*` tables.
- `DatabaseHealthProbe` adds `data["vector_dimension"]` (`"768"`). A `product_vectors` table of another dimension is an ERROR whose detail mentions 768.

### Application

- `RecordReceipt(uow_factory, clock, tolerances)`:
  - The receipt's `created_at` and `updated_at` are `clock.now()`. Everything happens in exactly one committed unit: a failure part-way leaves nothing behind.
  - `RecordReceiptResult.review` is the stored `ReviewItem` or `None`. `masked_pans` is the total number of card numbers masked across `ocr_text` and every line's `raw_text` and `description`.
  - Lines are linked to products for both outcomes, COMPLETED and NEEDS_REVIEW.
- Linking a line whose import product name is `name`:
  - It looks up the global alias `normalize_alias(name)`. On a miss it creates `Product(canonical_name=name, category=line.category, brand=None, size=None, tags=())` and that global alias with source `IMPORT`.
  - It also stores the store-specific `IMPORT` alias `normalize_alias(raw_text)` for the receipt's `store_key`. The raw text is masked first, so a card number never reaches an alias.
- Price observations are written only when the report is ok and `purchased_on` is known, and only for linked `ITEM` lines: deposit, discount and fee lines can be linked but get no observation. Fields: `observed_on = purchased_on` and the receipt's `store_key`. `unit_price` is the line's unit price, otherwise `line_total / quantity` rounded half up to the cent.
- The review item has reason `VALIDATION_FAILED` and `thread_id=None`. Its details are `{"issues": [{"code", "message", "expected", "actual", "position"}, ...]}`, in report order, with Money as `{"amount_cents", "currency"}`.
- Logging uses structlog. RecordReceipt emits one `receipt.recorded` event with `receipt_id`, `status`, `item_count` and `masked_pans`, and never any receipt text.
- `GetReceipt(uow_factory, tolerances)` returns `(Receipt, ArithmeticReport)`, recomputed with the injected tolerances. It raises `NotFoundError`.
- `ListReceipts(uow_factory)` is called as `(page, status, store)`. `store` goes through `normalize_store_name`.
- `Container` gains `uow_factory`, `record_receipt`, `get_receipt` and `list_receipts`. `build_container` still opens no database. All tolerances come from one `Tolerances()` in the container.

### HTTP API

- `created_at` is ISO-8601 with an offset. `purchased_on` is `YYYY-MM-DD` or null. `quantity` is the stored decimal text (`"0.452"`, `"1"`). `has_image` is `image_path is not None`. `ocr_text` is absent.
- The following return 422 `/problems/validation`: `limit` outside 1..100 or not an integer, a bad cursor, an unknown `status`, and a non-integer receipt id. An unknown id returns 404 `/problems/not-found` with `instance` set to the request path. `listReceipts` declares a 422 problem response and `getReceipt` a 404 problem response.
- `GET /api/categories` lists every category in enum order with distinct, non-empty labels. It works on an unmigrated database.
- OpenAPI component names: `MoneyOut`, `ReceiptSummaryOut`, `LineItemOut`, `ArithmeticIssueOut`, `ArithmeticReportOut`, `ReceiptDetailOut`, `ReceiptPageOut`, `CategoryOut`.

### CLI and import format

- `ReceiptImport.model_validate(data).to_draft()` builds the draft:
  - Strictness: cents fields are strict integers (no float, `"899"` or `true`). Unknown fields are rejected at both levels.
  - Formats: `purchased_on` is an optional ISO date, `currency` must match `^[A-Z]{3}$`, `payment_last4` is four digits, and `quantity` is a positive decimal string.
  - Defaults and mapping: `description` defaults to `raw_text`, and `position` is the list index. `product` fills `product_names`. `ocr_text` and `image_path` are `None`.
- `paper-trail receipts import FILE`:
  - Output: it configures logging like `db upgrade` and prints `receipt <id>: <status>` followed by one line per issue message. It exits 0 whenever the receipt is recorded.
  - Validation errors (unknown field, bad cents, malformed JSON) go to stderr and name the field, with exit 2 and no traceback. A missing FILE is a usage error (exit 2).
  - On an unmigrated database the command exits 1 with a hint to run `db upgrade`.
  - `--dry-run` prints `dry run: <status it would get>` and the issues, and writes nothing.
- `paper-trail receipts show ID` does not configure logging, so stdout is only the JSON document. That document equals `GET /api/receipts/{id}` (same response model). An unknown id exits 1 with the id on stderr and nothing on stdout.

### Frontend

- `src/features/receipts/money.ts` exports `formatMoney(money, locale?)`, built on `Intl.NumberFormat` with `style: "currency"` and the money's own currency. It is the only `style: "currency"` in `src/`, and no source uses `.toFixed(`.
- `src/features/receipts/dates.ts` exports `formatDate(isoDate, locale?)`. It uses `dateStyle: "medium"` on the calendar date in UTC, so the day never shifts in a time zone west of UTC. This file is not in the plan's list; the tests add it so the list and the detail page share one date formatter.
- Receipts tab:
  - Each row is a link to `/receipts/{id}` inside `<main>`. Its text includes the store, the formatted date, the formatted total and a status label (`needs_review` shown as "needs review"). Missing date or total never renders `NaN`, `undefined` or `null`.
  - While the first page loads, skeleton rows (`data-slot="skeleton"`) are shown.
  - The empty state heading is "No receipts yet".
  - When a sentinel at the end is observed by `IntersectionObserver`, the next page is fetched with `?cursor=`. Nothing more is fetched when `next_cursor` is null.
  - A failure shows a toast "Couldn't load receipts" (or "Could not ...") with a "Retry" action that refetches.
- Detail page `/receipts/$receiptId`:
  - Header: an `h1` with the store name, plus the date, total and status.
  - Items: a `<table>` whose column headers match description/item, qty/quantity, unit price and total, with quantity shown as sent.
  - Totals block: `Subtotal`, `Tax` and `Total`, each followed by its amount.
  - Arithmetic: when `ok` is false, the issue messages are listed. When `ok` is true, there is no "does not add up" or "issues found" copy.
  - A 404 shows a heading matching /not found/ inside `<main>`.

## Acceptance criteria coverage

| AC | What is checked | Tests |
|---|---|---|
| S1-AC-01 | ISO currency validation; int-only amounts; error hierarchy; `+`/`-`/neg; mismatch raises; `times(0.452)` on 1099 is 497 and half-away-from-zero cases; frozen, hashable, value equality; `zero`, `is_negative`, `sum_money`; property tests: exact integer arithmetic, `times` within half a cent | `tests/unit/domain/test_money.py` |
| S1-AC-02 | Exact enum members per PLAN 3.2 (plus `IssueCode`, `HitKind`); lowercase values through `str`/`json.dumps`; quantity `<= 0` rejected, fractional accepted; `payment_last4` format; `product_names` default; frozen slots dataclasses with tuple collections | `tests/unit/domain/test_models.py`, `tests/unit/interfaces/test_receipt_import_schema.py::test_s1_ac_24_quantity_*` |
| S1-AC-03 | Six published test PANs valid; every single-digit substitution invalid; non-digit, spaced and short input invalid; two-digit boundary; property test | `tests/unit/domain/rules/test_luhn.py` |
| S1-AC-04 | The plan's example; dash, unseparated, Amex grouping and punctuation-adjacent forms with separators preserved; count of several numbers; UPC, non-Luhn, dates, prices, phones, 20 digits, double spaces and masked text unchanged; 13 and 19 digit bounds; properties: idempotent, no Luhn-valid run left (independent oracle), only digits change and only to `*`, count matches the oracle | `tests/unit/domain/rules/test_masking.py`, `tests/pans.py` |
| S1-AC-05 | Consistent with and without subtotal, with discount, deposit and fee lines; Receipt accepted; 1-cent per-line tolerance and 2 cents rejected; weighed line rounding; exact codes for line math, items vs subtotal, subtotal+tax vs total (also without tax), items+tax vs total, negative ITEM/DEPOSIT/FEE, mixed currency (lines and totals), empty receipt; tolerances injectable; every issue at once; every message names line and amounts; property: consistent receipts are ok | `tests/unit/domain/rules/test_arithmetic.py` |
| S1-AC-06 | Nine alias variants normalize to `ks org eggs 24ct`; size tokens kept; properties: idempotent, whitespace collapsed, casefolded; ten store names to their key; store idempotence property | `tests/unit/domain/rules/test_normalization.py` |
| S1-AC-07 | 500 g/399 is 798/kg, 2 L/549 is 274.5/l, 12 each/600 is 50/each, several packs, ml, kg; lb and oz to kg; `None` without size or unit; exact `Decimal` | `tests/unit/domain/rules/test_unit_price.py` |
| S1-AC-08 | Limit default and inclusive bounds, rejection outside 1..100; round trip with microseconds; opaque URL-safe base64 format; UTC on decode; ten garbage cursors; property round trip | `tests/unit/domain/test_paging.py`, `tests/contract/test_receipt_repository.py::test_s1_ac_11_list_rejects_a_garbage_cursor` |
| S1-AC-09 | Head `0002` after `0001`; eight regular and three virtual tables; FTS5 options and columns; vec0 768 cosine; every index of PLAN 3.5 (incl. expression and partial); trigger counts and events; downgrade leaves nothing; upgrade-downgrade-upgrade identical; `compare_metadata` empty; metadata holds the eight tables; seed rows with PLAN column names; each enum CHECK accepts exactly the enum values; currency, last4, NOT NULL and FK constraints; store defaults; migrations import no app code; readiness checks the vector dimension | `tests/integration/test_migration_0002.py`, `tests/integration/test_schema_matches_metadata.py`, `tests/repo/test_migrations_frozen.py`, `tests/integration/test_database_health_probe.py::test_s1_ac_09_*`, `tests/integration/test_migrations.py` |
| S1-AC-10 | Each port suite runs for `fake` and `sqlite`: receipts (add/get/list/update_status/replace_contents), products and aliases, prices, corrections, jobs, reviews, vectors, plus the text index | `tests/contract/test_receipt_repository.py`, `test_product_repository.py`, `test_price_repository.py`, `test_correction_repository.py`, `test_job_queue.py`, `test_review_queue.py`, `test_vector_index.py`, `test_text_search_index.py` |
| S1-AC-11 | Every field round-trips exactly (USD, negative money, fractional and trailing-zero quantities, leap day, microseconds, enums typed); nullable fields; item order; newest first with id tie-break; instant order not text order; summary fields; limit, cursor, exactly-full page, empty, filters by status and store_key while paging; column types store text, reject floats and naive datetimes; quantity is TEXT in the table | `tests/contract/test_receipt_repository.py::test_s1_ac_11_*`, `tests/integration/test_sqlite_types.py` |
| S1-AC-12 | Exact alias match; store alias beats global, falls back to global; store alias not global; USER_CORRECTION kept against IMPORT/MATCHED/EXTRACTED (global and per store); equal or higher priority replaces in place; NULL store_key is a key | `tests/contract/test_product_repository.py::test_s1_ac_12_*` |
| S1-AC-13 | Receipt delete cascades to line items, observations, jobs, reviews (other receipt kept), corrections keep with NULL links; product delete nulls `line_items.product_id`, removes aliases, observations and the vector row (also invisible through the ports); line delete cascades to its observation; cascade removes FTS rows | `tests/integration/test_cascades.py`, `tests/integration/test_fts.py::test_s1_ac_13_*` |
| S1-AC-14 | Empty queue; oldest first with lease; future jobs wait; expired lease reclaimable (strict `<`), old owner loses it; extend by owner; non-owner extend/complete/fail/release raise `ConflictError` and change nothing; PENDING has no owner; unknown ids `NotFoundError`; complete; fail with retry until max then FAILED with `last_error`; fail without retry final; release keeps attempts; pending count; four threads drain 60 jobs with no double claim and no lock errors | `tests/contract/test_job_queue.py`, `tests/integration/test_job_queue_concurrency.py` |
| S1-AC-15 | Open stamps and fields; nested details JSON (floats, booleans, null, unicode) round-trip; second OPEN item conflicts; resolve/dismiss set status and `resolved_at`, second close conflicts, reopen allowed; unknown id; list_open paging and open_count | `tests/contract/test_review_queue.py` |
| S1-AC-16 | Description words; raw-text prefixes (`org egg`); AND semantics; diacritics; products by name, brand, tags; score order; limit; repository updates reflected; 17 FTS-syntax inputs return `()` without raising; operator words are prefixes; token-less queries; punctuation stripped. SQLite only: negated bm25 is positive; triggers follow SQL insert/update/delete | `tests/contract/test_text_search_index.py`, `tests/integration/test_fts.py` |
| S1-AC-17 | Dimension 768; wrong lengths rejected (upsert and query); ascending cosine distance with exact expected distances; identical dense vector at ~0; at most k; ties by product id; upsert replaces; delete; empty index | `tests/contract/test_vector_index.py` |
| S1-AC-18 | Commit visible to the next unit; missing commit and exception roll back (exception propagates); reads own writes; all-or-nothing across repositories, queue included | `tests/contract/test_unit_of_work.py` |
| S1-AC-19 | COMPLETED/DONE with report, one commit, clock stamps; draft fields copied, `store_key` normalized; products and IMPORT aliases on first sight; unnamed lines unlinked; reuse across stores; one observation per linked ITEM line (not deposit/discount); derived unit price; no observations without a date; one unit of work (failure leaves nothing); visible to later units | `tests/unit/application/test_record_receipt.py::test_s1_ac_19_*` |
| S1-AC-20 | NEEDS_REVIEW/NEEDS_REVIEW, exactly one OPEN VALIDATION_FAILED item, no observations, lines still linked; `details.issues` mirrors the report; tolerances injected | `tests/unit/application/test_record_receipt.py::test_s1_ac_20_*` |
| S1-AC-21 | Masked `ocr_text`, `raw_text`, `description` and `masked_pans`; aliases built from masked text; `receipt.recorded` log without text. On a real file: no Luhn-valid run in any cell of any table (FTS shadow tables included) and none in stdout/stderr at DEBUG | `tests/unit/application/test_record_receipt.py::test_s1_ac_21_*`, `tests/integration/test_record_receipt_masking.py` |
| S1-AC-22 | List and detail shapes (exact key sets, money objects, decimal string, dates, `has_image`, no `ocr_text`); arithmetic reflects later data changes; paging through all receipts; status and normalized store filters; 422 for limit/cursor/status, 404 with instance, 422 for non-int id; empty list; use cases with fakes (report recomputed on read with injected tolerances, NotFound, paging, store normalization, status filter, bad cursor); container wiring | `tests/e2e/test_receipts_api.py::test_s1_ac_22_*`, `tests/unit/application/test_get_and_list_receipts.py` |
| S1-AC-23 | Every category once, enum order, distinct non-empty labels; no database needed | `tests/e2e/test_receipts_api.py::test_s1_ac_23_*` |
| S1-AC-24 | Import then `GET /api/receipts` (the CLI in-process through Typer's runner, same DB); inconsistent import exits 0 as needs_review with issue text; `--dry-run` writes nothing (both outcomes); unknown fields and non-integer cents exit 2 naming the field; malformed JSON and missing file exit 2; unmigrated DB exits 1; `show` prints the API document, unknown id exits 1; `--help` lists the group, commands and `--dry-run`; import schema defaults, full mapping, sample file, 14 invalid inputs naming the field | `tests/e2e/test_import_and_query.py`, `tests/unit/interfaces/test_cli_receipts.py`, `tests/unit/interfaces/test_receipt_import_schema.py` |
| S1-AC-25 | Operation ids and problem responses in the app spec; schema names; committed `openapi.json` has the three operations and `schema.d.ts` types the schemas; committed spec equals the backend (Stage 0 drift test). `make api-check` itself is run by `make check` | `tests/e2e/test_receipts_api.py::test_s1_ac_25_*`, `frontend/tests/tooling/generated-api.test.ts`, `tests/e2e/test_openapi_contract.py::test_s0_ac_32_*` |
| S1-AC-26 | Skeletons while loading; empty state; rows with store, formatted date, total and status; null date/total; next page on scroll with the cursor, no extra request at the end; error toast with Retry that recovers; same-origin request. Detail: request path, header, item table, discount row, totals block, issues listed when not ok and absent when ok, 404 state; date formatter is time-zone safe | `frontend/src/features/receipts/receipt-list.test.tsx`, `receipt-detail.test.tsx`, `dates.test.ts` |
| S1-AC-27 | `-$2.50` in en-CA; more amounts, other currencies, other locale; no float artefacts; no `.toFixed(` in `src/`; `style: "currency"` only in `money.ts`; list and detail use `formatMoney` | `frontend/src/features/receipts/money.test.ts`, `frontend/tests/tooling/money-formatting.test.ts` |

## Criteria not fully automatable

- **S1-AC-25** (`make api-check` passes). This is a gate target, so `make check` verifies it. The tests check the spec and generated types it compares.
- **S1-AC-26** (look and feel). jsdom does no layout, so row density, skeleton shape, toast placement and touch targets are for the reviewer on a phone-sized viewport.
- No Stage 1 test touches the LLM host, the network or other containers.

## Notes from the implementation

- `tests/integration/test_migration_0002.py` read `sqlite_master` with `Result.tuples()`, which
  SQLAlchemy 2.1 deprecates; with warnings as errors every test of that module failed before
  reaching its assertion. It now builds the dict from the rows directly (same data, same checks).
- `src/test/pointer-capture.ts` (installed in `src/test/setup.ts`) gives jsdom no-op
  `setPointerCapture`/`releasePointerCapture`: sonner's toasts capture the pointer on
  pointerdown, so clicking the error toast's Retry action threw an unhandled error otherwise.
- `src/test/fixtures/receipts.ts` (listed above but missing from the interrupted run) was
  written: typed builders `cad`, `receiptSummary`, `receiptPage`, `lineItem`, `receiptDetail`.
- Tests added for behaviour the implementation pins down beyond the list above: a stored
  `Receipt` and a `PriceObservation` are in one currency (`CurrencyMismatchError`, since the
  schema keeps one currency per row); non-finite quantities are rejected; `Money.divided_by` and
  `Money.amount_text`; naive cursor times; a receipt without a total skips rules 3 and 4; an
  empty pack has no unit price; `StrEnumType` refuses plain strings; quantities in the import
  must be decimal strings; a blank product name links nothing and a known product gets no second
  global alias; review details keep a missing amount as `null`; and, for SQLite only
  (`tests/integration/test_sqlite_unit_of_work.py`), `commit()` outside the block, a failed
  `BEGIN IMMEDIATE` returning its connection, foreign-key violations not being reported as
  conflicts, and `replace_contents` of an unsaved receipt.
