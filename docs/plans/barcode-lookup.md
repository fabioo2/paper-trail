# Barcode and product catalog plan (codes on the receipt -> exact products)

Written 2026-11-08 by the planner. Status: **draft, awaiting owner approval.** This is **post-progress-report work**: nothing here blocks the progress report. It builds on `docs/plans/agent-core.md` (slices A-D as built), `docs/plans/extraction-accuracy.md` (S2 `split_item_code`) and ADR-0004, 0013, 0015, 0016, 0017. It adds ADR-0018.

## Summary (approve from here)

1. **Owner decision:** use the code printed on a receipt line to identify the product, not the LLM's reading of an abbreviation. On the 2026-11-08 receipts, enrichment v1 got about 40 % of product identities wrong, inventing brands: `06038306487 PC UNS BABYWIPES HMRJ` became "Pampers", `06340012135 CM CORN 7IN 6PK` (corn tortillas) became "Chiquita canned corn", and `CRV CHKKN PBLN BR` became "Curved Chicken".
2. **Three local catalog sources** sit behind one port, `ProductCatalog`, and are tried in this order:
   1. **Store catalog.** Codes kept in our own database (`product_codes`, migration 0004), filled by hand and learned from people's corrections. Example: `("costco", "1793017") -> product 19`.
   2. **PLU table.** Produce codes from a local file the owner provides (see 6).
   3. **Open Food Facts.** An offline Canadian subset for UPC/EAN codes.

   Anything that misses all three falls back to the existing alias, vector and LLM matching and to LLM enrichment, unchanged. The trace names the source: `store_catalog`, `plu`, `off` or `llm`.
3. **Open Food Facts offline.** The source is the nightly **CSV export** (tab-separated, gzip): food is **1.53 GB** (`Content-Length` 1,529,658,234, Last-Modified 2026-11-07), plus beauty 20 MB, products 6.4 MB and pet food. A one-pass stream filter keeps rows whose `countries_tags` contains `en:canada` (about **130 k** products) and only 7 fields. The result is a separate read-only SQLite file, `/data/catalog/off.sqlite`, of about **25-40 MB**. `make catalog` builds it in a container, writes to a temporary file and swaps it in atomically. It never writes to the repo.
4. **Pure code normalization** (`domain/rules/item_codes.py`). It reads the code from `raw_text`, drops the `(1)` quantity marker, computes the GS1 check digit for Loblaw's 11-digit UPC bodies (`06038306487` -> `060383064877` -> key `0060383064877`) and classifies each code as GTIN, PLU, IN_STORE, STORE_ITEM or NONE, using a per-store profile. At Costco every number is a store item number, including `1281` for milk.
5. **Matching:** a known code links the line exactly (new `MatchSource.CATALOG`) before the alias and vector steps. A PLU or OFF hit for an unknown code creates the product from the catalog. **Enrichment:** a catalog-described product needs **no LLM call** (name, brand and size come from the catalog; OFF `categories_tags` map to our 22 categories in code). If a category is unmapped, the model is asked for the category only.
6. **PLU:** IFPS publishes no open licence for its list (only "users assume all risk"; no redistribution terms found). So **no IFPS list is committed.** `make plu PLU_FILE=...` imports a CSV the owner exported from the IFPS PLU search into `/data/catalog/plu.csv`. PLU codes can also be curated or learned through the store catalog, so produce coverage grows without the file.
7. **Store catalog UX.** All entries point at a product:
   - `paper-trail catalog add/list/remove`;
   - `catalog adopt`, which promotes an existing product or proposes entries in bulk from lines already matched;
   - CSV `export`/`import`;
   - automatic learning when a person corrects a line (`items correct`) or names a product in a review.
8. **Existing data:** `catalog backfill` re-identifies the 38 products in the main database. It is a dry run by default, `--apply` writes, and it is idempotent. It respects person-made choices.
9. **Data-local rule:**
   - Building the catalog downloads public files and sends nothing.
   - The **online OFF fallback** is a setting, **default off**. When on, it sends only a GTIN, never receipt text.
   - **ODbL obligations:** attribution wherever OFF data is shown or reported, and share-alike only if the derived database is ever distributed. We never distribute it: each install builds its own.
10. **Expected coverage** on the 3 real receipts:
    - Superstore has 26 item lines: **16 are UPC lines OFF can look up**, 7 are PLU-range codes, 1 is a store-assigned 5-digit code (`62021`) and 2 are in-store random-weight codes.
    - Costco's 13 item lines (9 distinct item numbers) can come only from the store catalog.
    - The actual OFF hit rate is measured after `make catalog`.
11. **Estimate and cut line:** about 13.5 h in total, of which the core is about 10.5 h. Cut first: the online fallback, CSV import/export, the LLM category fallback, alias tax-flag clean-up, the review-resolve learning hook, `adopt --all`.

---

## 1. Data sources

### 1.1 Open Food Facts exports (researched 2026-11-08)

| Export | URL | Size | Notes |
|---|---|---|---|
| **CSV, food** (chosen) | `https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz` (302 to `openfoodfacts-ds.s3.eu-west-3.amazonaws.com`) | **1.53 GB** gz, about 9 GB raw | Nightly. UTF-8, **tab-separated**, 211 columns. Columns include `code`, `product_name`, `abbreviated_product_name`, `quantity`, `brands`, `categories_tags`, `countries_tags` and `product_quantity`. |
| CSV, beauty / products / pet food | `static.openbeautyfacts.org/...csv.gz`, `static.openproductsfacts.org/...csv.gz`, `static.openpetfoodfacts.org/...csv.gz` | 20 MB / 6.4 MB / small | Same format. **Needed:** baby wipes, dish pacs and similar items are not food. |
| JSONL | `.../openfoodfacts-products.jsonl.gz` | **13.4 GB** gz | Has `product_name_en`/`_fr`, but is 9x the download. Rejected. |
| Parquet (Hugging Face `openfoodfacts/product-database`) | `food.parquet` | **8.0 GB** (beauty 67 MB) | Has a per-language `product_name` list. DuckDB with httpfs could read only the needed columns over HTTP ranges, but that is a new dependency and a third-party host. Rejected for v1 and noted as the upgrade path if per-language names matter. |
| MongoDB dump / delta files | `.../openfoodfacts-mongodbdump.gz`, `.../delta/index.txt` | large / daily JSON | Deltas carry no deletions and need the full dump. Rejected. |

**Trade-off accepted:** the CSV `product_name` is in the product's main language only. Canadian products are mostly English or bilingual. `abbreviated_product_name` is the fallback when `product_name` is blank.

**Code format (OFF "barcode normalization" reference):** OFF stores `code` normalized in the database and in every export:
- leading zeros are stripped;
- 1-7 digits are padded to **8**;
- 9-12 digits are padded to **13**;
- 13 or more digits are kept.

So UPC-A `034000470693` is stored as `0034000470693`. Our key function `off_key()` applies exactly this rule, both to receipt GTINs and, defensively, to CSV codes at build time.

### 1.2 The Canadian subset (`/data/catalog/off.sqlite`)

```sql
CREATE TABLE catalog_products (            -- WITHOUT ROWID, keyed by OFF's normalized code
  code TEXT PRIMARY KEY, dataset TEXT NOT NULL,   -- food | beauty | products | petfood
  name TEXT, brands TEXT, quantity TEXT,          -- as OFF has them
  product_quantity TEXT, product_quantity_unit TEXT,
  categories TEXT NOT NULL DEFAULT '[]'           -- JSON list of en: categories_tags, at most 12
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- meta: built_at, per-export url + Last-Modified + ETag + rows read/kept/malformed,
--       country tag, schema version, the ODbL notice text
```

- **Filter:** keep a row when `en:canada` is in its `countries_tags` (`CATALOG__COUNTRY`) and it has a name or a brand. Rows without a code, with non-digit codes or with malformed fields are counted and skipped.
- **Size estimate:** `ca.openfoodfacts.org` listed **130,317** products on 2026-11-08. At about 200-260 bytes per row plus the primary-key B-tree, the file is about **25-40 MB**. The build fails below `CATALOG__MIN_ROWS` (20,000), so a truncated download never replaces a good catalog.
- **Why a separate file and not tables in the app DB (amends ADR-0004):**
  - the app DB stays small and its backups personal;
  - a refresh is an atomic `os.replace` with no migration and no write lock on the app DB;
  - the catalog is public reference data, rebuildable at any time;
  - it is opened read-only (`mode=ro`), so a bug cannot write to it.

### 1.3 `make catalog` (download + filter, in a container)

```
make catalog          = $(RUN) cli catalog build            # OFF subset -> /data/catalog/off.sqlite
make barcodes         = alias of make catalog (the name in the brief)
make plu PLU_FILE=... = $(RUN) cli catalog plu-import "/data/catalog/import/$(PLU_FILE)"
make pt ARGS="catalog status"                               # sources, built_at, rows, Last-Modified
```

- `catalog build` is a CLI command in the existing image (`cli` service; stdlib `urllib`, `gzip`, `csv`, `sqlite3`; **no new dependency**). Each export is downloaded to `/data/catalog/.download/<name>.csv.gz` and the download resumes with HTTP `Range` after a dropped connection. Each file is stream-filtered in one pass (`csv.field_size_limit` raised, `QUOTE_NONE`, tab), the rows go into `off.sqlite.tmp`, and the build runs `PRAGMA integrity_check`, checks the row floor, `ANALYZE`s and then `os.replace`s the file. The downloads are deleted unless `--keep-download` is given.
- **Refresh:** run `make catalog` whenever wanted (monthly is plenty). It sends `If-None-Match`/`If-Modified-Since` from `meta` and skips an unchanged export (`--force` re-downloads). The adapter reopens the file when its mtime changes, so a running worker picks up a new catalog without a restart.
- **Cost:** about 1.6 GB of transient disk on the data volume and one CPU for an estimated 5-10 min (the container runs with `cpus: 1`, keeping the owner's media services responsive). Nothing goes into the repo, and `/data` is already git-ignored.
- **Privacy:** the build only GETs public static files. No receipt data, code or user agent detail beyond `PaperTrail/<version> (catalog build)`.

### 1.4 PLU table

- **Ranges:**
  - IFPS PLU codes are **3000-4999** (conventional) and the same codes with prefix **9** (`94046`, organic).
  - Prefix 8 was retired by IFPS (the 83000-84999 range is IFPS-reserved).
  - Retailer-assigned numbers exist in IFPS-defined blocks.
  - Any other 4-5 digit number is the store's own code (Superstore's `62021`).
- **Licence:** the IFPS site offers search and an Excel export of the full list. It states "each user ... assumes all risk", but no open licence and no redistribution terms could be found. **Decision: commit no IFPS data.** The repo ships only `tests/data/catalog/plu_fixture.csv`, a few placeholder rows for tests. If the owner wants a committed seed, they email `plu@ifpsglobal.com` (owner decision D-4).
- **Import:**
  - `catalog plu-import FILE` reads the owner's export (columns detected by header: PLU, commodity, variety, size; the rest is ignored) and writes the normalized `/data/catalog/plu.csv` (`code,name,organic`).
  - It is loaded into memory by `PluCatalog` (a few thousand rows).
  - Without the file the PLU source simply misses.
- **Organic:** `9xxxx` looks up `xxxx` and adds "Organic " to the name and tag `organic`.
- Learned and curated PLU mappings live in `product_codes` (scheme `plu`), so they work with or without the file.

### 1.5 Optional online OFF fallback (default OFF)

- `CATALOG__ONLINE_FALLBACK=false`. When true, a GTIN that misses locally is fetched with `GET {ONLINE_URL}/api/v2/product/{gtin}?fields=code,product_name,brands,quantity,product_quantity,product_quantity_unit,categories_tags`, timeout 5 s. The request carries a custom User-Agent, as OFF requires, and **only the barcode**: no store, no line text, no price.
- A hit becomes the product's code in `product_codes` (source `off`), so the same barcode is never fetched twice. Misses are remembered for the process's lifetime only. Errors and timeouts count as misses and never fail a receipt.
- Recorded as ADR-0018 decision 4, which amends ADR-0015's "only outbound request is Ollama" to "... unless the owner turns on the barcode-only catalog fallback". **Below the cut line.**

---

## 2. Code normalization (pure domain, `domain/rules/item_codes.py`)

```python
class CodeKind(StrEnum): GTIN, PLU, IN_STORE, STORE_ITEM, NONE
class CodeScheme(StrEnum): GTIN = "gtin"; PLU = "plu"; STORE = "store"   # how a code is stored

@dataclass(frozen=True, slots=True)
class ItemCode:
    kind: CodeKind
    printed: str                 # digits as printed, marker removed: "05700001285"
    keys: tuple[str, ...]        # lookup keys, best first: GTIN -> OFF keys; PLU -> ("4046",);
                                 # STORE/IN_STORE -> (printed,)
    scheme: CodeScheme | None    # None for NONE
    organic: bool = False        # PLU with prefix 9

def gs1_check_digit(body: str) -> str          # mod-10, weights 3,1,3,... from the right
def is_valid_gtin(code: str) -> bool           # 8, 12, 13, 14 digits with a valid check digit
def off_key(gtin: str) -> str                  # OFF normalization (1.1)
def parse_item_code(raw_text: str, store_key: str) -> ItemCode | None   # None: no leading code
def split_item_code(raw_text: str) -> tuple[str | None, str]            # extended, see below
```

**Extraction:**
- `split_item_code` (normalization.py, ADR-0017) also accepts a leading quantity marker `(N)`, N being 1-2 digits: `"(1)05700001285 CRV CHKKN PBLN BR MRJ"` -> `("05700001285", "CRV CHKKN PBLN BR MRJ")`. Today this line is sent to enrichment with its code, and `normalize_alias` keeps `(1)05700001285` in the alias.
- `parse_item_code` uses it and returns None when no leading code is found.
- The Costco reference lines (`0000392208 / 1362394`) are DISCOUNT lines and are never looked up.

**Classification:** the store profile is a module constant keyed by `normalize_store_name`. `costco` maps to "every code is STORE_ITEM"; the default profile applies to every other store:

| Digits | Rule (default profile) | Kind | Keys |
|---|---|---|---|
| 4 in 3000-4999; 5 = `9` + 3000-4999 | IFPS PLU range | PLU (`organic` for 9-prefix) | (`4046`,) |
| other 4-6 | store's own code (`62021`) | STORE_ITEM | (printed,) |
| 7, first digit `2` | Loblaw scale / meat-counter code (`2100580`, `2348420`) | IN_STORE | (printed,) |
| other 7 | store code | STORE_ITEM | (printed,) |
| 8 | valid EAN-8 -> GTIN; else STORE_ITEM | GTIN / STORE_ITEM | (`off_key`,) |
| 11 | UPC-A body without check digit (Loblaw). First digit `2` (variable measure) or `4` (in-store) -> IN_STORE; `5` (coupon) -> NONE; else GTIN = body + `gs1_check_digit(body)` | GTIN | (`off_key("0"+upc)`,) |
| 12 | valid UPC-A check -> GTIN; else EAN-13 body + computed check. Both readings are offered when both are plausible, the valid-check one first | GTIN | 1-2 keys |
| 13 | valid EAN-13 -> GTIN, except prefixes `02`, `04`, `20`-`29` (GS1 restricted circulation) -> IN_STORE; invalid -> NONE | GTIN / IN_STORE | (`off_key`,) |
| 14 | valid GTIN-14 with a leading `0` -> its EAN-13 | GTIN | 1 key |
| <4, >14 | not a code | NONE | () |

Worked check digits:

| Code | Check digit | Key |
|---|---|---|
| `03400047069` | 3 (OFF's documented example, `0034000470693`) | `0034000470693` |
| `06038306487` | 7 | `0060383064877` |
| `05889165231` | 0 | `0058891652310` |
| `64843610117` | 7 | `0648436101177` |
| `05700001285` | 4 | `0057000012854` |
| `06340012135` | 5 | `0063400121355` |

**Alias tax flags (cuttable):**
- Superstore prints `MRJ` and `HMRJ` after every item, so aliases are `pc uns babywipes hmrj`.
- Add `mrj` and `hmrj` to `_TAX_FLAGS`. Add **only observed tokens**, never a pattern: `PJ` in `TH 2PC PJ` (pyjamas) must survive.
- This changes `normalize_alias`, so the backfill (5) re-normalizes `product_aliases.alias` and `corrections.alias` idempotently, merging duplicates (newest `created_at` wins).

---

## 3. Architecture

### 3.1 Port and adapters

```python
# ports/catalog.py
type CatalogSource = Literal["store_catalog", "plu", "off"]


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    source: CatalogSource
    key: str  # the code that hit
    product_id: int | None  # set when the code is already one of OUR products
    name: str | None
    brand: str | None
    size_value: Decimal | None
    size_unit: Unit | None
    categories: tuple[str, ...]  # OFF en: tags (empty for PLU / store)
    dataset: str | None  # off: food | beauty | products | petfood
    organic: bool = False


class ProductCatalog(Protocol):
    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None: ...
```

| Adapter | Where | Hit means |
|---|---|---|
| `adapters/sqlite/store_catalog.py` `StoreCatalog(uow_factory)` | app DB, `product_codes` | `(scheme, store_key or '', key)` is ours: `product_id` set, described from the product. `source="store_catalog"` for scheme store, `"plu"`/`"off"` for learned plu/gtin rows, for the trace |
| `adapters/catalog/plu.py` `PluCatalog(path)` | `/data/catalog/plu.csv`, in memory | PLU kind only |
| `adapters/catalog/off.py` `OffCatalog(path)` | `/data/catalog/off.sqlite`, read-only, reopened on mtime change | GTIN kind; tries each key in order |
| `adapters/catalog/off_online.py` `OnlineOffCatalog(...)` | optional, see 1.5 | GTIN kind |
| `adapters/catalog/composite.py` `CompositeCatalog(*catalogs)` | first non-None wins | |

- **Order:** store catalog, then PLU, then OFF, then online OFF if enabled.
- A missing `off.sqlite` or `plu.csv` is a miss, with one `catalog.missing` warning per process, never an error. The health endpoint lists the catalog's state (built_at, rows), which is informational and never makes the app "not ready".
- Fake for tests: `tests/fakes/catalog.py`, `FakeCatalog({key: CatalogProduct})`, which records its calls.
- Mapping from OFF fields: `size_value`/`size_unit` come from `product_quantity` + `product_quantity_unit` (`g`, `kg`, `ml`, `l`, `cl` -> ml x10, `oz`, `lb`), else from parsing `quantity` (`"1.5 kg"`, `"6 x 355 ml"` -> None), else None.

### 3.2 `product_codes` and migration 0004

```sql
CREATE TABLE product_codes (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  scheme TEXT NOT NULL CHECK (scheme IN ('gtin','plu','store')),
  store_key TEXT NOT NULL DEFAULT '',          -- '' = any store; NOT NULL so UNIQUE works
  code TEXT NOT NULL,                          -- gtin: off_key; plu: 4 digits; store: as printed
  source TEXT NOT NULL CHECK (source IN
    ('manual','import','adopted','correction','review','receipt','off','plu','backfill')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK ((scheme = 'store') = (store_key <> '')),
  UNIQUE (scheme, store_key, code)
);
CREATE INDEX ix_product_codes_product ON product_codes(product_id);
-- line_items.match_source gains 'catalog': SQLite cannot alter a CHECK, so 0004 copies the
-- column to a temporary one, drops it, re-adds it with the new CHECK, copies back and drops the
-- temporary column (0003's downgrade already proves DROP COLUMN works on it). No table rebuild,
-- so the line_items FTS triggers are untouched.
```

- **Why a codes table and not `products.barcode`** (the first brief):
  - one product can be known by several codes: the same item at Costco (item number) and at Superstore (UPC), or a UPC alongside an EAN-13 reading;
  - the store catalog needs a store-scoped key anyway;
  - one table gives one lookup, one uniqueness rule ("a code names at most one product") and one place for provenance.
- No `line_items.item_code` column: the code is recomputed from `raw_text` by the pure parser whenever it is needed (learning, backfill), so stored data cannot drift from the rule.
- Domain and repository:
  - `ProductCode` model;
  - `ProductCodeRepository` (`find(scheme, store_key, code)`, `for_product(id)`, `list(store_key=None, scheme=None)`, `upsert(code, *, replace: bool)`, `remove(...)`) on the `UnitOfWork`;
  - SQLAlchemy Core table in `adapters/sqlite/schema.py`;
  - mappers;
  - `MatchSource.CATALOG`. The OpenAPI spec and TS client are regenerated because `match_source` is exposed.
- **Write policy:**
  - Machine writes (`receipt`, `off`, `plu`, `backfill`) **never replace** an existing row (`ON CONFLICT DO NOTHING`, logged `catalog.code_conflict`).
  - Person writes (`manual`, `import --replace`, `adopted`, `correction`, `review`) replace, because a person outranks the machine.

### 3.3 Tool `catalog_lookup` (ADR-0013 amendment)

```python
class CatalogLookupInput(_Model):
    raw_text: StrictStr  # the line's raw text; parsed by the tool (like product_lookup)
    store_key: StrictStr


class CatalogEntryOut(_Model):
    source: Literal["store_catalog", "plu", "off"]
    key: str
    name: str | None
    brand: str | None
    size_value: str | None
    size_unit: Unit | None
    category: Category | None  # mapped in code (4.2); None = unmapped
    tags: list[str]  # e.g. "organic", leaf category names (<= MAX_TAGS)


class CatalogLookupOutput(_Model):
    code: ItemCodeOut | None  # kind, printed, keys, scheme
    product: ProductOut | None  # our product, when the code is already ours
    entry: CatalogEntryOut | None
```

- Allowlist: `{MATCHING, ENRICHMENT}`. It is read-only, like `product_lookup`.
- It runs in-process and over MCP. `ToolServices` gains `catalog: ProductCatalog`, wired in `bootstrap`.
- The `tools list --role matching` output and the MCP role snapshot gain one tool.

---

## 4. Pipeline integration

### 4.1 Matching (`agents/nodes/matching.py`)

New per-line order. Steps 2-4 of the old order are unchanged:

0. A line a person named in a review stays skipped. DEPOSIT, FEE and DISCOUNT lines and generic aliases are skipped as now.
1. **`catalog_lookup(raw_text, store_key)`:**
   - `product` is set -> link it, `source: catalog`, decision `catalog_source` = the entry's source.
   - Only `entry` is set (PLU/OFF hit, a code no product has yet) -> `source: new`, `new_product_name` = the entry name, or `"<brand> <name>"` when the brand is not in the name. The decision carries `code` (scheme, store_key, key) and the `catalog` entry.
   - No code, or a miss -> carry on.
2. `product_lookup` (memory, then alias).
3. Vector auto-link and LLM confirm band.
4. New product. When the line has a GTIN, PLU or store code and `CATALOG__LEARN_NEW_CODES` is true, the decision also carries `code`, with source `receipt`. A product created *for* that code is, by construction, that code's product.

Supporting changes:
- **`save_matches`:** `MatchDecisionIn` gains `code: ItemCodeRef | None` (`scheme`, `store_key`, `key`, `source`) and `catalog: CatalogEntryOut | None`. `record_matches` inserts each code with the machine write policy (3.2) in the same unit of work. Alias/vector/LLM links **never** attach codes: a vector match is a guess, not an identity.
- **`commit_matches`** keeps each created line's `catalog` entry in `state["created"]` (absent in old checkpoints).
- **Trace:** the matching node ends with one `NOTE` per receipt, for example "catalog 9 (off 6, plu 3) · alias 1 · new 2". The tool events already carry each hit.

### 4.2 Enrichment (`agents/nodes/enrichment.py`)

- For each created product:
  - It has a `catalog` entry -> `ProductEnrichment` built in code by `catalog_enrichment(entry)`: name, brand, size, the mapped category and tags (`organic`, leaf category names, at most `MAX_TAGS`). There is **no LLM call**. The trace records a `NOTE` "described from off 0060383064877", data `{"source": "off"}`.
  - Its category is unmapped -> one `enrichment/<version>` request with `line: <catalog name>` (the clean name instead of the abbreviation), and **only its `category`** is used (cuttable; otherwise `other`).
  - No entry -> today's LLM enrichment, unchanged; the trace data says `source: llm`.
- `save_enrichment` is unchanged: a person's category memory still wins, and the text is masked as before.
- **Category map** (`domain/rules/catalog_categories.py`, pure): an ordered rule list from most specific to least, first match wins.

  | OFF tag (examples) | Category |
  |---|---|
  | `en:alcoholic-beverages` | alcohol |
  | `en:baby-foods`, `en:baby-wipes`, `en:diapers` | baby |
  | `en:frozen-foods` | frozen |
  | `en:dairies`, `en:eggs`, `en:cheeses`, `en:yogurts` | dairy_eggs |
  | `en:meats`, `en:poultries`, `en:seafood`, `en:fishes` | meat_seafood |
  | `en:breads`, `en:tortillas`, `en:pastries` | bakery |
  | `en:prepared-meals`, `en:sandwiches` | deli_prepared |
  | `en:snacks`, `en:sweet-snacks`, `en:salty-snacks`, `en:confectioneries`, `en:popcorn` | snacks_candy |
  | `en:beverages`, `en:waters` | beverages |
  | `en:fresh-fruits`, `en:fresh-vegetables`, `en:fruits`, `en:vegetables` (non-canned) | produce |
  | `en:groceries`, `en:condiments`, `en:sauces`, `en:canned-foods`, `en:cereals-and-potatoes`, `en:noodles`, `en:sugars`, `en:spreads`, `en:desserts` | pantry |

  Defaults by dataset: `beauty` -> personal_care, `petfood` -> pet. `products` needs its tags (`en:cleaning` -> household). A PLU -> produce. The table is data in code, so the test-author pins it by table.

### 4.3 Learning (store catalog part c)

- **`items correct`** (`CorrectLineItem`): after the relink, if `parse_item_code(line.raw_text, store_key)` has a scheme, the code is upserted to the target product with source `correction` and the **person write policy** (it replaces a wrong mapping). This happens in the same unit of work and is logged `catalog.learned`.
- **Review answers** (`RecordReceipt` `_linked` with `product_names`): the same, with source `review`. Cuttable.
- Corrections made before this feature are picked up by the backfill (5).

---

## 5. Store catalog CLI and existing data

All commands accept `--run NAME` (that run's database, as elsewhere) and print one table. `--store` goes through `normalize_store_name`, so `costco` and `"Costco Wholesale"` are the same store. Scheme selection is automatic: `--code` is parsed with that store's profile, so a GTIN or PLU code becomes a global entry and a store code a store entry.

```
paper-trail catalog add --store S --code C (--product ID | --name N [--brand B]
                        [--size 1.5 --unit kg] [--category pantry]) [--replace]
paper-trail catalog list [--store S] [--scheme gtin|plu|store] [--json]
paper-trail catalog remove --store S --code C            # the product stays
paper-trail catalog adopt --store S --code C --product ID # = add --product, source "adopted"
paper-trail catalog adopt --store S --all-matched [--apply]
        # proposes every (store, code) whose ITEM lines at that store all link to ONE product
        # (conflicts listed, never applied); dry run by default
paper-trail catalog export [--store S] > file.csv         # store,code,scheme,product_id,name,brand,
paper-trail catalog import FILE [--apply] [--replace]      #   size_value,size_unit,category
paper-trail catalog lookup "<raw line text>" --store S   # debug: parse + which source hits (local)
paper-trail catalog build [--force] [--keep-download] | plu-import FILE | status
paper-trail catalog backfill [--apply]
```

- **`add --name`** creates the product through `new_product` and applies the given descriptors. `import` with a name and no `product_id` does the same; with a `product_id` it links.
- **Sample data:** only `docs/samples/store-catalog.sample.csv` (placeholder `SAMPLE ITEM` rows) is committed. Real catalogs stay in the data volume (ADR-0015).

**`catalog backfill`** handles existing data: 38 products in the main database, dry run by default, idempotent. For each product with linked ITEM lines:
1. Parse each line's code with its receipt's store. Lines of one product with different codes are reported, not merged.
2. Look the code up in PLU, then OFF. The local store catalog is skipped, since the code is not ours yet.
3. Plan the changes:
   - attach the code (source `backfill`, machine policy);
   - on a catalog hit, **re-describe** the product with `catalog_enrichment`, **except** fields a person chose: a CATEGORY correction keeps its category, and a product a person created by name (`items correct --new-product`, review `product_names`) keeps its name;
   - re-embed changed products;
   - set the lines' `match_source` to `catalog`? No. History stays as it was; only products change.
4. Report two products whose lines carry the same code as **merge candidates**. Merging is not automatic: it is a later command.
5. Optionally, when the tax-flag change ships, re-normalize aliases and corrections (2).

The output is a before/after table: line code | kind | source | name, brand, category before -> after. `--apply` writes everything in one unit of work per product. Re-embedding happens outside the transaction, as in `save_enrichment`. A second `--apply` reports "0 changes".

---

## 6. Settings (`PAPER_TRAIL_CATALOG__*`, new `CatalogSettings`)

| Variable | Default | Why |
|---|---|---|
| `CATALOG__ENABLED` | `true` | `false` skips step 1 of matching entirely (A/B comparison) |
| `CATALOG__OFF_PATH` | `<data_dir>/catalog/off.sqlite` | 1.2 |
| `CATALOG__PLU_PATH` | `<data_dir>/catalog/plu.csv` | 1.4 |
| `CATALOG__COUNTRY` | `en:canada` | subset filter |
| `CATALOG__EXPORTS` | the food, beauty, products and pet food CSV URLs | 1.1 |
| `CATALOG__MIN_ROWS` | `20000` | build sanity floor |
| `CATALOG__LEARN_NEW_CODES` | `true` | 4.1 step 4 (owner decision D-2) |
| `CATALOG__LLM_CATEGORY_FALLBACK` | `true` | 4.2 |
| `CATALOG__ONLINE_FALLBACK` | **`false`** | 1.5, ADR-0018 |
| `CATALOG__ONLINE_URL` / `__ONLINE_TIMEOUT_S` | `https://world.openfoodfacts.org` / `5` | 1.5 |

The store code profiles and the PLU ranges are module constants, not settings. A CLI flag `process --no-catalog` sets `CATALOG__ENABLED=false` (it is also refused on `--resume`, like the other configuration flags).

---

## 7. ADR-0018 and amendments

**ADR-0018 "Products are identified by the code on the receipt"** decides:
1. Code before language: store catalog, then PLU, then OFF, then LLM.
2. Codes in `product_codes` with a machine/person write policy, and no `products.barcode`.
3. OFF offline as a separate read-only file, built by an explicit owner command from the CSV export.
4. An online fallback that is barcode-only and default off.
5. No IFPS data in the repo.
6. ODbL attribution.

Alternatives considered:
- the LLM with better prompts (it cannot know that `CM` is a tortilla brand);
- the full OFF dump or the API per receipt (data-local, size);
- Parquet with DuckDB;
- UPCitemdb or other commercial APIs (data-local, licences).

**Amendments:**
- **ADR-0004:** a second, read-only SQLite file for public reference data.
- **ADR-0015:**
  - `catalog build` downloads public exports and sends no data;
  - the optional barcode-only fallback;
  - attribution text.
- **ADR-0013:** the `catalog_lookup` tool, allowlisted to matching and enrichment.
- **ADR-0017:** `split_item_code` drops the `(N)` marker, and check digits are computed in code.

**ODbL obligations** (Open Food Facts database ODbL, contents DbCL; images CC-BY-SA, which we do not use):
- **(a) Attribution:** "Contains information from Open Food Facts, made available under the Open Database License (ODbL)". It goes in the README, in `catalog status`, in `meta`, and in any report or screenshot that shows catalog-derived product data (the course report: a "Produced Work", so a notice is required).
- **(b) Share-alike:** the subset is a Derivative Database. It is never committed or distributed, and each install builds its own, so no share-alike obligation arises. If it were ever published, it would have to be offered under the ODbL.
- **(c) No DRM or technical restrictions** on anything shared.

---

## 8. Acceptance criteria

- **BC-AC-01** `gs1_check_digit`/`is_valid_gtin`:
  - `03400047069` -> `3`; `06038306487` -> `7`; `05889165231` -> `0`;
  - valid EAN-8, EAN-13 and GTIN-14 accepted; a wrong check digit rejected.
- **BC-AC-02** `off_key`:
  - `034000470693` -> `0034000470693`;
  - 13 digits unchanged;
  - `00012345` style codes of 7 digits or fewer are padded to 8;
  - leading zeros beyond 13 stripped.
- **BC-AC-03** `split_item_code` and `parse_item_code` (table-driven):
  - `(1)05700001285 CRV ...` -> GTIN, key `0057000012854`;
  - `06038306487 PC UNS BABYWIPES HMRJ` -> GTIN `0060383064877`;
  - `4046 AVOCADO SMALL` -> PLU `4046`;
  - `94046 ...` -> PLU `4046`, organic;
  - `62021 APPLES` -> STORE_ITEM;
  - `2100580 LN GRND BF & PK` -> IN_STORE;
  - an 11-digit code starting with `2` or `4` -> IN_STORE, with `5` -> NONE;
  - at store `costco`: `1793017`, `18584` and `1281` -> STORE_ITEM;
  - `12 EGGS` and `2% MILK` -> None;
  - 12- and 13-digit cases per section 2;
  - the existing ADR-0017 split cases are unchanged.
- **BC-AC-04** Category map:
  - pinned table of tags -> category;
  - first rule wins;
  - unknown tags -> None;
  - dataset defaults;
  - PLU -> produce.
- **BC-AC-05** `catalog_enrichment` builds a valid `ProductEnrichment` from an OFF entry:
  - size from `product_quantity`/unit, and from `quantity` text;
  - unparseable -> no size;
  - brand kept separate;
  - organic PLU -> "Organic ..." + tag.
- **BC-AC-06** Migration 0004:
  - upgrade and downgrade round-trip;
  - `product_codes` constraints (UNIQUE, the scheme/store CHECK, cascade on product delete);
  - existing `match_source` values survive the column swap and `catalog` is accepted;
  - the `line_items` FTS triggers still fire;
  - the schema-matches-metadata test passes;
  - the frozen migrations 0001-0003 are unchanged.
- **BC-AC-07** `ProductCodeRepository` contract:
  - find, upsert, list and remove;
  - the machine policy keeps an existing row and logs the conflict;
  - the person policy replaces it.
- **BC-AC-08** Adapters:
  - `OffCatalog` over a fixture file: the key-order hit; a missing file -> miss plus one warning; read-only (a write attempt fails); reopens after the file is replaced.
  - `PluCatalog`: hits, organic, missing file -> miss.
  - `CompositeCatalog`: order (a store catalog row shadows OFF).
- **BC-AC-09** `catalog build` against a **local fixture HTTP server** serving tiny gzip CSVs (tabs, a malformed row, a non-Canadian row, a row without a name):
  - only Canadian named rows are kept;
  - `meta` is filled;
  - atomic replace, so a failure mid-build leaves the old file intact;
  - the row floor refuses a short build;
  - a 304/ETag skip;
  - Range resume;
  - no request goes anywhere except the configured URLs.
- **BC-AC-10** The `catalog_lookup` tool:
  - allowed for matching and enrichment, denied for the others (exit 5 in `tools call`);
  - the input parses `raw_text`;
  - the output validates;
  - in-process and MCP results are identical.
- **BC-AC-11** Matching (fake LLM, fake catalog):
  - a known code -> `catalog` link with no `product_lookup`, vector or LLM call;
  - an OFF hit -> NEW named from the catalog, with code and entry in the decision;
  - a miss -> today's path, unchanged (existing matching tests pass as they are);
  - a DISCOUNT line and person-named lines are never looked up;
  - `CATALOG__ENABLED=false` -> no `catalog_lookup` call.
- **BC-AC-12** `save_matches`/`record_matches`:
  - codes inserted in the same transaction;
  - idempotent on resume;
  - alias/vector/LLM links attach no code;
  - NEW with `LEARN_NEW_CODES` attaches source `receipt`;
  - a conflicting code is not stolen.
- **BC-AC-13** Enrichment:
  - a created product with an entry -> saved description equals `catalog_enrichment`, **0 LLM calls**, trace `source: off`;
  - an unmapped category -> exactly one LLM call and only its category is used;
  - with the fallback off -> `other`;
  - no entry -> the existing behaviour and tests;
  - a person's category memory still wins.
- **BC-AC-14** Learning:
  - `items correct` on a Costco line upserts `("costco","1793017")` to the target (replacing a previous mapping);
  - the next receipt with that line links via `catalog`;
  - a line without a code learns nothing;
  - the review `product_names` case is the same (cuttable).
- **BC-AC-15** CLI `catalog add/list/remove/adopt`:
  - store normalization;
  - automatic scheme;
  - refusal without `--replace`;
  - `adopt --all-matched` dry run lists proposals and conflicts and writes nothing;
  - `--apply` writes;
  - `export` then `import` round-trips (cuttable).
- **BC-AC-16** `catalog backfill`:
  - the dry run writes nothing;
  - `--apply` attaches codes and re-describes catalog hits;
  - person-chosen category and name are kept;
  - merge candidates are reported;
  - a second `--apply` -> 0 changes.
- **BC-AC-17** Data-local:
  - no network in `process` with default settings (a test asserts no `urllib` opener is constructed and the online adapter is not wired);
  - with `ONLINE_FALLBACK=true`, the fake HTTP server sees only `/api/v2/product/<gtin>` with no other receipt data, and a timeout counts as a miss.
- **BC-AC-18** Settings defaults per section 6; `--no-catalog` is refused on `--resume`.
- **BC-AC-19** (cuttable) Tax flags: `normalize_alias("PC UNS BABYWIPES HMRJ") == "pc uns babywipes"`, `"TH 2PC PJ"` is unchanged, and the backfill re-normalizes and merges aliases idempotently.
- **BC-AC-20** `make ci` is green with coverage of at least 90 %. The OpenAPI spec and TS client are regenerated for `match_source: catalog`. The graph snapshot is unchanged (no new node).

## 9. Test list for the test-author

No real network, no real OFF, no real IFPS data, no receipt codes from the owner's database except the codes already quoted in this plan. Fixtures use OFF's documented example `034000470693`, synthetic GTINs with valid check digits, and `SAMPLE` names.

| Suite | File |
|---|---|
| unit/domain | `test_item_codes.py` (AC-01..03, table-driven); `test_catalog_categories.py` (AC-04); `test_catalog_enrichment.py` (AC-05); `test_normalization.py` additions (marker, AC-19) |
| integration | `test_migration_0004.py` (AC-06); `test_product_codes_sqlite.py` (AC-07); `test_off_catalog.py` and `test_plu_catalog.py` (AC-08, fixture files built in `tmp_path`); `test_catalog_build.py` (AC-09, `http.server` on loopback in a thread); `test_catalog_backfill_sqlite.py` (AC-16) |
| contract | `test_product_catalog_contract.py`: the same cases against `FakeCatalog`, `CompositeCatalog`(fakes) and the SQLite adapters |
| unit/tools | `test_catalog_lookup_tool.py` (AC-10), registry allowlist table update |
| unit/agents | `test_matching_catalog.py` (AC-11), `test_enrichment_catalog.py` (AC-13), graph-level run through `agent_harness` (AC-12) |
| unit/application | `test_correction_learns_code.py` (AC-14) |
| unit/interfaces | `test_cli_catalog.py` (AC-15, 18) |
| unit | `test_config_barcode.py` (AC-18), `test_catalog_privacy.py` (AC-17) |
| data | `tests/data/catalog/off_fixture.csv` (about 10 rows), `plu_fixture.csv`, `store-catalog.sample.csv` |

Pin these in `docs/tests/barcode-lookup.md` as the other plans do.

## 10. Experiment E-BC (after `make catalog`)

1. `make catalog`: record the download size, build time, rows kept and file size, which bear out the 130 k / 25-40 MB estimates.
2. `make pt ARGS="catalog backfill"` (dry run, main database): the before/after table for the 38 products and the hit counts. Expected eligible codes on today's three receipts:

| Receipt | Item lines | GTIN (OFF) | PLU range | Store's own 4-5 digit | In-store 7-digit | Store item (Costco) |
|---|---|---|---|---|---|---|
| Superstore 1 | 18 | 10 | 5 (3121, 3156, 4029, 4048, 4053) | 1 (62021) | 2 (2100580, 2348420) | - |
| Superstore 2 | 8 | 6 | 2 (4046, 4068) | - | - | - |
| Costco | 13 (9 distinct) | - | - | - | - | 13 (only via the store catalog) |
| **Total** | 39 | **16 lookup-able in OFF** | **7, hit only with `plu.csv` or curated** | 1 | 2 | 13 |

The actual OFF hit rate (of 16) is reported here. Loblaw private label (`060383...`) coverage in OFF is the open question. **If it is below 50 %, the store catalog covers the gap:** `catalog add` works for GTINs too.

3. `--apply`, then `make process RECEIPTS=<superstore> RUN_NAME=bc ARGS="--reprocess"` against a copy, plus a `--no-catalog` arm. Compare:
   - match sources;
   - LLM calls (enrichment calls expected to fall from 1 per new product to 0 for hits);
   - time;
   - **product identity errors judged by the owner, before (about 40 %) and after**, including the three named cases.
4. Costco: `catalog adopt --store costco --all-matched`, fix names with `catalog add --replace`, then `--apply`. Re-process the Costco receipt in a run. Expected: 13/13 item lines `store_catalog` and 0 matching or enrichment LLM calls.

Every screenshot or report table that shows catalog data carries the ODbL attribution line.

## 11. Files

| File | Change |
|---|---|
| `domain/rules/item_codes.py` | new (section 2) |
| `domain/rules/catalog_categories.py` | new (4.2) |
| `domain/rules/normalization.py` | `(N)` marker in `split_item_code`; tax flags (cuttable) |
| `domain/models.py`, `domain/enums.py` | `ProductCode`; `MatchSource.CATALOG`, `CodeScheme`, `CodeKind` |
| `ports/catalog.py`, `ports/repositories.py`, `ports/unit_of_work.py` | `ProductCatalog`, `CatalogProduct`; `ProductCodeRepository` |
| `adapters/sqlite/{schema,mappers,product_code_repository,store_catalog,unit_of_work}.py` | table, repo, store catalog |
| `adapters/catalog/{off,plu,composite,off_online,build}.py` | adapters and the build pipeline |
| `migrations/versions/0004_product_codes.py` | section 3.2 |
| `application/catalog.py` | add, adopt, import/export, backfill, `catalog_enrichment` |
| `application/matching.py`, `application/corrections.py`, `application/receipts.py` | codes in `record_matches`; learning hooks |
| `tools/{models,handlers,registry}.py` | `catalog_lookup`; `MatchDecisionIn.code` and `.catalog` |
| `agents/nodes/{matching,enrichment}.py`, `agents/nodes/supervisor.py` (`commit_matches`) | section 4 |
| `config.py`, `bootstrap/` | `CatalogSettings`, wiring |
| `interfaces/cli/catalog.py`, `main.py`, `process.py` | the `catalog` sub-app, `--no-catalog` |
| `Makefile` | `catalog`, `plu` targets |
| `docs/adr/0018-...md`; ADR-0004, 0013, 0015, 0017 amendments; README (attribution, `make catalog`); `docs/PLAN.md` deltas; `docs/tests/barcode-lookup.md` | |

## 12. Effort and cut line

| Piece | Estimate |
|---|---|
| Domain codes, check digits, category map, `catalog_enrichment` + tests | 1.5 h |
| Migration 0004, `product_codes` repo, `MatchSource.CATALOG`, OpenAPI regen | 1.5 h |
| `catalog build` (stream filter, meta, atomic, resume) + `make catalog` | 1.5 h |
| Port, OFF/PLU/store/composite adapters, fake, `catalog_lookup` tool | 1 h |
| Matching + `save_matches` codes + catalog enrichment | 2 h |
| Store catalog CLI (add/list/remove/adopt) + `items correct` learning | 1 h |
| Backfill | 1 h |
| ADR-0018, amendments, README, experiment | 1 h |
| **Core subtotal** | **about 10.5 h** (an agent-pipeline estimate, including review) |
| Cuttable: online fallback 1 h, CSV import/export 0.5 h, LLM category fallback 0.3 h, tax-flag alias clean-up 0.5 h, review learning hook 0.3 h, `adopt --all-matched` 0.4 h | about 3 h |

**Cut order (first to go):**
1. Online fallback.
2. CSV import/export.
3. `adopt --all-matched`.
4. Tax-flag clean-up.
5. Review learning hook.
6. LLM category fallback (unmapped -> `other`).

**Minimum viable:**
- domain codes;
- migration 0004;
- `make catalog`;
- the OFF and store-catalog adapters plus the tool;
- matching and enrichment from the catalog;
- `catalog add/list/remove/adopt --product`;
- `items correct` learning;
- backfill.

## 13. Owner decisions needed

- **D-1** Approve `product_codes` (several codes per product) instead of `products.barcode`.
- **D-2** `LEARN_NEW_CODES=true`: a product created for a line takes that line's code automatically, including Costco item numbers (shown as source `receipt` in `catalog list`). The alternative is that Costco entries come only from `add`, `adopt` and corrections.
- **D-3** The backfill re-describes existing products from OFF, overwriting LLM-written names and brands but never person-chosen ones.
- **D-4** PLU: import your own IFPS export (default), or ask IFPS for permission to commit a seed.
- **D-5** Keep the online fallback in scope (default off) or drop it.
