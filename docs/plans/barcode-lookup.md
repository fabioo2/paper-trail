# Barcode and product catalog plan (codes on the receipt -> exact products, no guessed identities)

Written 2026-11-08 by the planner. Revised 2026-11-08 after three owner inputs: "stop guessing product identity", "Costco data by assisted manual entry from costco.ca", and "the local LLM agents must stay meaningful". Status: **approved by the owner 2026-11-08, with the recommended answer to every decision (D-1 to D-16).**

This is **post-progress-report work**. It is built **after** `docs/plans/discounts-and-paid-prices.md`, which lands three things this plan uses:
- migration 0004;
- the `(N)` marker in `split_item_code`;
- the price observation discount fields that this plan's line moves carry along.

It builds on `docs/plans/agent-core.md` (slices A-D as built), `docs/plans/extraction-accuracy.md` (S2 `split_item_code`) and ADR-0004, 0009, 0013, 0015, 0016, 0017. It adds ADR-0018.

## Summary (approve from here)

1. **Owner decision (2026-11-08): stop guessing product identity from abbreviations.** On the 2026-11-08 receipts, enrichment v1 got about 40 % of product identities wrong:
   - `06038306487 PC UNS BABYWIPES HMRJ` became "Pampers";
   - `06340012135 CM CORN 7IN 6PK` (corn tortillas) became "Chiquita canned corn";
   - `CRV CHKKN PBLN BR` became "Curved Chicken".

   **New rule:** a line's product is identified by its code. When no data source knows the code, the product is stored as printed: it is keyed by its code (a store item number, GTIN or PLU) and named after the printed text without the code. Brand, size and category stay empty. **No model invents a product's name, brand, size or category.**
2. **Product fields come only from data sources, and each field records its source** (new `products.field_sources`). The sources:
   - **the person**: the store catalog, filled by hand, e.g. `("costco", "1362394") -> product 31`;
   - **the PLU table**: a local file the owner provides;
   - **Open Food Facts offline**: a Canadian subset, for UPC/EAN codes;
   - **the printed receipt text**: the fallback name.
3. **Two kinds of linking, kept apart.**
   - **Within a store, the code is the identity.** A (store, code) pair, or a GTIN or PLU, links by exact match only, with no embeddings or model.
   - **Across stores, codes are unrelated.** Costco item numbers and Superstore UPCs never match. Products at different stores are linked for price comparison by **embeddings plus the model's confirmation**, but **only between confirmed products**: products whose names come from the store catalog, a person, PLU or OFF. Printed text that nothing has identified is never a candidate. A cross-store link puts both products in one **comparison group** (`product_groups`). Each store keeps its own product, codes and price history.
4. **Matching within a store, no model.**
   - A coded line links to the code's product. If the code is unknown, a PLU or OFF entry for it creates the product. If no source knows it either, a new printed-text product keyed by the code is created.
   - A line with no code links through a person's correction, then an exact alias at that store, else gets a new printed-text product.
   - The "falls back to LLM enrichment" path, vector auto-linking within a store and the LLM category fallback are removed.
5. **The Lookup/Enrichment agent** keeps its place in the agent architecture.
   - It calls `catalog_lookup` through its MCP allowlist and fills only the fields a source supplies, each with its source.
   - It uses the model only on data in front of it: **(a)** parsing a product title the owner pasted (`catalog add costco 3139415 --from-title "Zespri SunGold Kiwi, 2 lb"`), shown for confirmation before saving; **(b)** tidying OFF records (ALL-CAPS names, odd size strings such as `31/40`, categories the code mapping cannot place).
   - Every value the model returns must quote the title or record it came from, and code checks each quote.
6. **Category of a product no source knows: null** (D-6). The UI shows it as "uncategorized".
7. **Costco: assisted manual entry.**
   - No open Costco Canada item-number dataset exists. costco.ca's terms exclude automated collection, and the site has bot protection. **The app makes no request to costco.ca.**
   - `catalog todo --store costco` lists each unknown code with its printed text, how often it was bought, the last price paid, and a costco.ca search link (`https://www.costco.ca/CatalogSearch?keyword=<code>`). The owner opens the link in their own browser.
   - The owner then runs `catalog add costco <code> "<name>"` (or `--from-title "<pasted title>"`). That one entry names the product for every past and future line with that (store, code).
   - Warehouse item numbers often, but not always, match the online numbers, and many warehouse-only items (produce, bakery) are not online. For those, the shelf tag, the receipt text or a fan site such as cocowest.ca is a reference the owner reads by hand.
8. **Existing data: `catalog backfill`** (dry run by default, `--apply`, idempotent). It:
   - attaches each line's code;
   - **splits** products whose lines carry different codes (links made earlier by vector or LLM matching);
   - resets every field a model wrote to the printed text or null;
   - applies PLU and OFF where they know the code;
   - keeps every choice a person made;
   - drops the vectors of unconfirmed products.
9. **Kept from the first draft:**
   - `product_codes` (now migration **0005**);
   - the offline OFF build (`make catalog`);
   - PLU import, with no IFPS data committed;
   - the `catalog_lookup` tool;
   - code normalization with check digits;
   - the data-local rules and ODbL attribution;
   - the optional online fallback (default off).
10. **Where the model still works** (section 0.1): vision extraction (which reads the codes), feedback retries, cross-store matching, title parsing and OFF tidying. Identity within a store, naming unknown products, discount attribution, amounts and arithmetic are code.
11. **Order:** the discounts plan is built first. This plan uses its migration 0004, its `(N)` marker, and its discount fields (which line moves carry along).
12. **Estimate and cut line:** about 21.75 h in total, of which about 18.75 h is core. Cut first, in order:
    1. the online fallback;
    2. `adopt --all-matched`;
    3. interactive `catalog fill`;
    4. bulk `catalog link-stores`;
    5. alias re-normalization;
    6. the review learning hook.

Owner decisions: D-1 to D-16 (section 13).

---

## 0. What changed from the first draft

| First draft | Now |
|---|---|
| A miss falls back to alias, vector and LLM matching, and to LLM enrichment, unchanged | Within a store: code, then correction, then exact alias, else a printed-text product, with no model. Embeddings and the model only relate **confirmed** products **across** stores (section 4.4) |
| LLM enrichment writes name, brand, size, category | The model only parses text it is given (a pasted title, an OFF record), quoting it; code checks every quote (sections 4.2, 5.2) |
| LLM category fallback (`CATALOG__LLM_CATEGORY_FALLBACK`) | Removed. An unmapped OFF category may be placed by OFF tidying only with a quote from the record; otherwise it stays null |
| `CATALOG__LEARN_NEW_CODES` setting | No setting: a product created for a coded line always takes the code |
| New product named after `line.description` (written by the extraction model) | Named after `printed_name(raw_text)`. `description` stays on the line as a reading aid |
| Backfill re-describes from OFF, else keeps LLM fields | Backfill resets every machine-written field, splits products whose lines carry different codes, then applies data sources (section 5.5) |
| Migration 0004 | 0005 (`down_revision = "0004"`) |
| `(N)` marker in `split_item_code` added here | Already landed with the discounts plan |
| Tax flags `mrj`/`hmrj`: cuttable | Core (display names); alias re-normalization stays cuttable |
| Costco only through the store catalog | Assisted manual entry with costco.ca search links; the app makes no request to costco.ca |

### 0.1 Where the LLM still works

| Where | Model | What it does | Why it is safe |
|---|---|---|---|
| Vision extraction (`vision/v1`) | qwen2.5vl | Copies the receipt, **including the item codes**, which identity within a store relies on | ADR-0017: values copied as printed and checked in code; arithmetic and completeness checks. A misread code creates a new printed-text product; it never produces a wrong link |
| Feedback retries | text / vision | Retries an extraction with the issues code found | Unchanged (ADR-0017) |
| Cross-store matching (`matching/v2`, section 4.4) | text model + `nomic-embed-text` | Decides whether two **confirmed** products at different stores are the same thing | Only confirmed names; distance limits; size and brand checks in code; every decision traced; a person's decision always wins |
| Title parsing (`catalog_title/v1`, section 5.2) | text model | Splits a product title the owner pasted into fields | Every value must quote the title (checked in code); the owner confirms before saving |
| OFF tidying (`catalog_normalize/v1`, section 4.2) | text model | Fixes casing, odd size strings and unmapped categories | Every value must quote the OFF record (checked in code); stored with source `off_llm` |

**Done by code, with no model:**
- identity within a store ((store, code), exact alias, a person's correction);
- the name of an unknown product (printed text) and its category (null);
- discount attribution (discounts plan);
- amounts, dates and taxes (ADR-0017);
- the receipt arithmetic.

**Roadmap idea, not in scope:** a natural-language price-history agent ("what did I pay for coffee this year"). It would turn a question into a read-only query over `price_observations` and comparison groups, using regular and paid prices. It would be a fifth agent with read-only tools, and it gets its own plan.

---

## 1. Data sources

### 1.1 Open Food Facts exports (researched 2026-11-08)

| Export | URL | Size | Notes |
|---|---|---|---|
| **CSV, food** (chosen) | `https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz` (302 to `openfoodfacts-ds.s3.eu-west-3.amazonaws.com`) | **1.53 GB** gz, about 9 GB raw | Nightly. UTF-8, **tab-separated**, 211 columns. Columns include `code`, `product_name`, `abbreviated_product_name`, `quantity`, `brands`, `categories_tags`, `countries_tags` and `product_quantity`. |
| CSV, beauty / products / pet food | `static.openbeautyfacts.org/...csv.gz`, `static.openproductsfacts.org/...csv.gz`, `static.openpetfoodfacts.org/...csv.gz` | 20 MB / 6.4 MB / small | Same format. **Needed:** baby wipes, dish pacs and similar items are not food. |
| JSONL | `.../openfoodfacts-products.jsonl.gz` | **13.4 GB** gz | Has `product_name_en`/`_fr`, but is 9x the download. Rejected. |
| Parquet (Hugging Face `openfoodfacts/product-database`) | `food.parquet` | **8.0 GB** (beauty 67 MB) | Has a per-language `product_name` list. DuckDB with httpfs could read only the needed columns over HTTP ranges, but that is a new dependency and a third-party host. Rejected for v1; noted as the upgrade path if per-language names matter. |
| MongoDB dump / delta files | `.../openfoodfacts-mongodbdump.gz`, `.../delta/index.txt` | large / daily JSON | Deltas carry no deletions and need the full dump. Rejected. |

**Trade-off accepted:** the CSV `product_name` is in the product's main language only. Canadian products are mostly English or bilingual. `abbreviated_product_name` is the fallback when `product_name` is blank. A French-only name stays French: OFF tidying may not translate (D-15).

**Code format (OFF "barcode normalization" reference):** OFF stores `code` normalized, in the database and in every export:
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
  - a refresh is an atomic `os.replace`, with no migration and no write lock on the app DB;
  - the catalog is public reference data that can be rebuilt at any time;
  - it is opened read-only (`mode=ro`), so a bug cannot write to it.

### 1.3 `make catalog` (download + filter, in a container)

```
make catalog          = $(RUN) cli catalog build            # OFF subset -> /data/catalog/off.sqlite
make barcodes         = alias of make catalog
make plu PLU_FILE=... = $(RUN) cli catalog plu-import "/data/catalog/import/$(PLU_FILE)"
make pt ARGS="catalog status"                               # sources, built_at, rows, Last-Modified
```

- **Build:** `catalog build` is a CLI command in the existing image (`cli` service; stdlib `urllib`, `gzip`, `csv`, `sqlite3`; **no new dependency**).
  1. Each export is downloaded to `/data/catalog/.download/<name>.csv.gz`. After a dropped connection the download resumes with HTTP `Range`.
  2. Each file is stream-filtered in one pass (`csv.field_size_limit` raised, `QUOTE_NONE`, tab), and the kept rows go into `off.sqlite.tmp`.
  3. The build runs `PRAGMA integrity_check`, checks the row floor, runs `ANALYZE`, then `os.replace`s the file.
  4. The downloads are deleted unless `--keep-download` is given.
- **Refresh:** run `make catalog` whenever wanted (monthly is plenty). It sends `If-None-Match`/`If-Modified-Since` from `meta` and skips an unchanged export (`--force` re-downloads). The adapter reopens the file when its mtime changes, so a running worker picks up a new catalog without a restart.
- **Cost:** about 1.6 GB of transient disk on the data volume and one CPU for an estimated 5-10 min. The container runs with `cpus: 1`, which keeps the owner's media services responsive. Nothing goes into the repo, and `/data` is already git-ignored.
- **Privacy:** the build only GETs public static files. It sends no receipt data and no code, and its user agent says only `PaperTrail/<version> (catalog build)`.

### 1.4 PLU table

- **Ranges:**
  - IFPS PLU codes are **3000-4999** (conventional), and the same codes with prefix **9** (`94046`, organic).
  - Prefix 8 was retired by IFPS (the 83000-84999 range is IFPS-reserved).
  - Retailer-assigned numbers exist in IFPS-defined blocks.
  - Any other 4-5 digit number is the store's own code (Superstore's `62021`).
- **Licence:** the IFPS site offers search and an Excel export of the full list. It states that "each user ... assumes all risk", but no open licence and no redistribution terms could be found.
  - **Decision: commit no IFPS data.** The repo ships only `tests/data/catalog/plu_fixture.csv`, a few placeholder rows for tests.
  - If the owner wants a committed seed, they email `plu@ifpsglobal.com` (D-4).
- **Import:**
  - `catalog plu-import FILE` reads the owner's export (columns detected by header: PLU, commodity, variety, size; the rest is ignored) and writes the normalized `/data/catalog/plu.csv` (`code,name,organic`).
  - `PluCatalog` loads it into memory (a few thousand rows).
  - Without the file, the PLU source simply misses.
- **Organic:** `9xxxx` looks up `xxxx`, adds "Organic " to the name and adds the tag `organic`.
- Learned and curated PLU mappings live in `product_codes` (scheme `plu`), so they work with or without the file.

### 1.5 Optional online OFF fallback (default OFF)

- `CATALOG__ONLINE_FALLBACK=false`. When true, a GTIN that misses locally is fetched with `GET {ONLINE_URL}/api/v2/product/{gtin}?fields=code,product_name,brands,quantity,product_quantity,product_quantity_unit,categories_tags` (timeout 5 s). The request carries a custom User-Agent, as OFF requires, and **only the barcode**: no store, no line text, no price.
- A hit becomes the product's code in `product_codes` (source `off`), so the same barcode is never fetched twice. Misses are remembered for the process's lifetime only. Errors and timeouts count as misses and never fail a receipt.
- Recorded as ADR-0018 decision 5. It amends ADR-0015's "only outbound request is Ollama" to "... unless the owner turns on the barcode-only catalog fallback". **Below the cut line.**

### 1.6 Costco Canada item numbers (researched 2026-11-08)

Costco receipts print Costco's own item numbers (`3139415 GOLD KIWI`, `1281 1% MILK`), not GTINs, so OFF and PLU cannot name them.

| Candidate | Finding | Usable? |
|---|---|---|
| An open or licensed Costco Canada item-number dataset | None found. Searches return only commercial scrapers (Apify actors) and paid datasets (Actowiz, Bright Data, ScrapeHero); none states an open licence | No |
| `thehesiod/costco-mcp` (MIT, on GitHub) | Resolves item numbers through Costco's **undocumented** product GraphQL endpoint (`ecom-api.costco.com`) with account tokens. Its own README says use "may violate Costco's Terms of Service". It refers to costco.com, not costco.ca | No: terms, and it sends data out (ADR-0015) |
| costco.ca, fetched by the app | Terms and Conditions of Use (June 1, 2024): the licence excludes "use of any data mining, bots, spiders, automated tools or similar data gathering and extraction methods ... to scrape or collect any information from the Sites" and "the collection, use or disclosure of any product listings, prices or descriptions". The site also has bot protection | No |
| costco.ca search page, opened by a person | Viewing is allowed "for informational and non-commercial, personal use only". The app only prints a link; the owner opens it | Yes, as a manual reference |
| cocowest.ca and similar fan sites | Manual reference only. Their terms were not reviewed here, and the data-local rule applies | Only read by hand |
| OFF | Keyed by GTIN; Costco receipts carry no GTIN | No, for Costco lines |

**Decision: assisted manual entry.**
- The app sends no request to costco.ca or to any fan site. The catalog code has no HTTP client for these hosts, and a test enforces it (BC-AC-17).
- For each Costco code no source knows, the CLI prints a costco.ca search URL built from the code (section 5.1). The owner opens it in their own browser, reads the product page if there is one, and enters the product with `catalog add`, typing the name or pasting the title for `--from-title`.

**Known limits** (stated in the CLI help and the README):
- **(a)** A warehouse item number on the receipt often matches the costco.ca item number, but not always. A search may find nothing, or a different pack size. The owner checks the page against the printed text.
- **(b)** Warehouse-only items (produce, bakery, many fresh items) are usually not online. The owner names them from the shelf tag, the receipt text or a fan site, by hand.
- **(c)** The link is a convenience only. The app copies nothing from the page; a title is used only when the owner pastes it.

If the owner later finds a dataset whose licence allows personal use, `catalog import` (CSV) loads it.

Sources: apify.com Costco scraper listings; actowizsolutions.com, brightdata.com and scrapehero.com Costco datasets; glama.ai/mcp/servers/thehesiod/costco-mcp; costco.ca/terms-and-conditions-of-use.html.

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
def printed_name(raw_text: str) -> str         # display name of a product no source knows
def lookup_url(store_key: str, code: str) -> str | None   # printed search link, never requested
STORE_SEARCH_URLS = {"costco": "https://www.costco.ca/CatalogSearch?keyword={code}"}
```

**Extraction:**
- `split_item_code` (normalization.py, ADR-0017) already drops a leading `(N)` quantity marker; that landed with the discounts plan. `parse_item_code` uses it and returns None when no leading code is found.
- The Costco reference lines (`0000392208 / 1362394`) are DISCOUNT lines. The discounts plan's `discount_reference` reads them; they are never looked up in a catalog.

**`printed_name`** removes the leading code and any trailing tax-flag or price tokens (the tokens `normalize_alias` drops), keeps case and abbreviations, and is never empty (it falls back to `raw_text`):
- `"06038306487 PC UNS BABYWIPES HMRJ"` -> `"PC UNS BABYWIPES"`;
- `"3139415 GOLD KIWI"` -> `"GOLD KIWI"`;
- `"(1)05700001285 CRV CHKKN PBLN BR MRJ"` -> `"CRV CHKKN PBLN BR"`.

**`lookup_url`** fills the store's template with the code as printed (leading zeros kept, URL-quoted). It returns None for stores without a template. Only Costco has one.

**Classification:** the store profile is a module constant keyed by `normalize_store_name`. `costco` maps to "every code is STORE_ITEM"; the default profile applies to every other store:

| Digits | Rule (default profile) | Kind | Keys |
|---|---|---|---|
| 4 in 3000-4999; 5 = `9` + 3000-4999 | IFPS PLU range | PLU (`organic` for the 9 prefix) | (`4046`,) |
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

**Tax flags (core):**
- Superstore prints `MRJ` and `HMRJ` after every item. Add `mrj` and `hmrj` to `_TAX_FLAGS`, so that `printed_name` and aliases drop them.
- Add **only observed tokens**, never a pattern: `PJ` in `TH 2PC PJ` (pyjamas) must survive.
- Re-normalizing the stored `product_aliases.alias` and `corrections.alias` idempotently (merging duplicates, newest `created_at` wins) is **cuttable** (section 5.5 step 8).

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
    quantity_text: str | None  # OFF `quantity` as printed, for tidying (4.2)
    organic: bool = False


class ProductCatalog(Protocol):
    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None: ...
```

| Adapter | Where | A hit means |
|---|---|---|
| `adapters/sqlite/store_catalog.py` `StoreCatalog(uow_factory)` | app DB, `product_codes` | `(scheme, store_key or '', key)` is ours: `product_id` set, described from the product. The trace source is `"store_catalog"` for scheme store, and `"plu"`/`"off"` for learned plu/gtin rows |
| `adapters/catalog/plu.py` `PluCatalog(path)` | `/data/catalog/plu.csv`, in memory | PLU kind only |
| `adapters/catalog/off.py` `OffCatalog(path)` | `/data/catalog/off.sqlite`, read-only, reopened on mtime change | GTIN kind; tries each key in order |
| `adapters/catalog/off_online.py` `OnlineOffCatalog(...)` | optional, see 1.5 | GTIN kind |
| `adapters/catalog/composite.py` `CompositeCatalog(*catalogs)` | the first non-None wins | |

- **Order:** store catalog, then PLU, then OFF, then online OFF if enabled.
- **Missing files:** a missing `off.sqlite` or `plu.csv` is a miss, with one `catalog.missing` warning per process, never an error. The health endpoint lists the catalog's state (built_at, rows); this is informational and never makes the app "not ready".
- **Fake for tests:** `tests/fakes/catalog.py`, `FakeCatalog({key: CatalogProduct})`, which records its calls.
- **Size from OFF fields:**
  - first `product_quantity` + `product_quantity_unit` (`g`, `kg`, `ml`, `l`, `cl` -> ml x10, `oz`, `lb`);
  - else parsed from `quantity` (`"1.5 kg"`; `"6 x 355 ml"` -> None);
  - else None, which OFF tidying may resolve (4.2).

### 3.2 Migration 0005 (`0005_product_codes.py`, `down_revision = "0004"`)

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

-- line_items.match_source gains 'catalog': SQLite cannot alter a CHECK, so 0005 copies the
-- column to a temporary one, drops it, re-adds it with the new CHECK, copies back and drops the
-- temporary column (0003's downgrade already proves DROP COLUMN works on it). No table rebuild,
-- so the line_items FTS triggers are untouched.

-- Field provenance: JSON object field -> source for canonical_name, brand, size, category, tags.
-- Sources: 'receipt_text', 'person', 'plu', 'off', 'off_llm', 'legacy_model'.
ALTER TABLE products ADD COLUMN field_sources TEXT NOT NULL DEFAULT '{}';
-- The migration stamps every existing product 'legacy_model' for each non-null field, so nothing
-- a model wrote before this plan counts as confirmed; the backfill (5.5) resolves them.

-- Cross-store comparison groups (4.4).
CREATE TABLE product_groups (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
ALTER TABLE products ADD COLUMN group_id INTEGER REFERENCES product_groups(id) ON DELETE SET NULL;
CREATE TABLE product_group_decisions (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  other_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  decision TEXT NOT NULL CHECK (decision IN ('same','different')),
  source TEXT NOT NULL CHECK (source IN ('vector','llm','person')),
  distance REAL, confidence REAL, created_at TEXT NOT NULL,
  CHECK (product_id < other_product_id),
  UNIQUE (product_id, other_product_id)
);
CREATE INDEX ix_products_group_id ON products(group_id);
CREATE INDEX ix_product_group_decisions_other ON product_group_decisions(other_product_id);
```

**Why a codes table and not `products.barcode`:**
- one product can be known by several codes (a UPC alongside an EAN-13 reading; a person's `adopt` of a second code);
- the store catalog needs a store-scoped key anyway;
- one table gives one lookup, one uniqueness rule ("a code names at most one product") and one place for provenance.

There is no `line_items.item_code` column: the code is recomputed from `raw_text` by the pure parser whenever it is needed, so stored data cannot drift from the rule.

**Domain and repository:**
- models `ProductCode`, `ProductGroupDecision`;
- `FieldSource(StrEnum)`; `Product.field_sources: Mapping[str, FieldSource]`; `Product.group_id`;
- pure `is_confirmed(product)`: true when the `canonical_name` source is `person`, `plu`, `off` or `off_llm`;
- `ProductCodeRepository` (`find`, `for_product`, `list`, `upsert(code, *, replace)`, `remove`) and `ProductGroupRepository` (`decision(a, b)`, `record(decision)`, `group_of`, `join(a, b)`, `members(group_id)`) on the `UnitOfWork`;
- SQLAlchemy Core tables in `adapters/sqlite/schema.py`, and mappers;
- `MatchSource.CATALOG`.

The OpenAPI spec and TS client are regenerated (`match_source`, `field_sources`, `group_id`).

**Write policy:**
- Machine writes (`receipt`, `off`, `plu`, `backfill`) **never replace** an existing row (`ON CONFLICT DO NOTHING`, logged `catalog.code_conflict`).
- Person writes (`manual`, `import --replace`, `adopted`, `correction`, `review`) replace it, because a person outranks the machine.
- A `person` field in `field_sources` is never overwritten by a machine source.

### 3.3 Tools (ADR-0013 amendment)

**`catalog_lookup`** (read-only), allowlist `{MATCHING, ENRICHMENT}`:

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
    quantity_text: str | None
    categories: list[str]  # OFF tags, for tidying
    category: Category | None  # mapped in code (4.2); None = unmapped
    tags: list[str]  # e.g. "organic", leaf category names (<= MAX_TAGS)


class CatalogLookupOutput(_Model):
    code: ItemCodeOut | None  # kind, printed, keys, scheme
    product: ProductOut | None  # our product, when the code is already ours
    entry: CatalogEntryOut | None
```

**`describe_from_catalog`** (supervisor only) replaces `save_enrichment`, which is removed from the registry.
- Input: `product_id`, `fields` (name, brand, size_value, size_unit, category, tags) and `sources` (one per given field).
- It writes only the fields that have a source, never replaces a `person` field, applies a person's CATEGORY correction for the line's alias and store (source `person`), masks free text, and embeds the product when it becomes confirmed.

**`product_similarity_search`** (allowlist `{MATCHING}`) gains `exclude_stores: list[str]` and `confirmed_only: bool` (default true). The handler drops unconfirmed products and products with a line at any excluded store.

**`save_group_decisions`** (supervisor only) records cross-store decisions and joins groups (4.4). It is idempotent.

`ToolServices` gains `catalog: ProductCatalog`, wired in `bootstrap`. Every tool runs in-process and over MCP. The `tools list --role` output and the MCP role snapshots change accordingly.

---

## 4. Pipeline integration

### 4.1 Matching within a store (`agents/nodes/matching.py`), no model

Per ITEM line, in order:

0. **Skipped lines.** A line a person named in a review stays skipped. DEPOSIT, FEE and DISCOUNT lines and generic aliases are skipped as now.
1. **A line with a code** (`parse_item_code` gives a scheme) calls `catalog_lookup(raw_text, store_key)`:
   - `product` set -> link it, `source: catalog`;
   - only `entry` set (PLU/OFF knows the code, we do not yet) -> `source: new`, with `new_product_name` = the entry's name (`"<brand> <name>"` when the brand is not in the name); the decision carries `code` and `catalog`;
   - miss -> `source: new`, with `new_product_name = printed_name(raw_text)`; the decision carries `code` (source `receipt`).

   A coded line gets **no `product_lookup`, no similarity search and no model call**: the code is the identity. A person's correction of a coded line is learned as a code (4.3), so memory still wins, through the store catalog.
2. **A line without a code:** `product_lookup` (a person's correction, then an exact alias at that store, then a global alias), else `source: new` with `printed_name(raw_text)`. No similarity search and no model.
3. **One product per code.** Lines of one receipt with the same code create **one** product: `plan_matches` keys `to_create` by `(scheme, store_key, code)` for coded lines, and by alias for uncoded lines as today.

**Supporting changes:**
- `MatchDecisionIn` gains `code: ItemCodeRef | None` (`scheme`, `store_key`, `key`, `source`) and `catalog: CatalogEntryOut | None`.
- `record_matches` inserts the code with the machine write policy in the same unit of work. An alias link never attaches a code.
- Products created from a PLU/OFF entry are confirmed, so they get a vector. Printed-text products get none, so `record_matches` calls the embedding model only for confirmed new products.
- `commit_matches` keeps each created line's `catalog` entry in `state["created"]` (absent in old checkpoints).
- **Trace:** one NOTE per receipt, e.g. "catalog 9 (store 5, off 3, plu 1) · alias 1 · new 4 (printed text 3, off 1)".

### 4.2 The Lookup/Enrichment agent (`agents/nodes/enrichment.py`)

For each product this receipt created (`state["created"]`, the first line of each):

**(a) Lookup, no model.** The agent calls `catalog_lookup` through its allowlist (in-process or MCP), and `catalog_fields(entry) -> (fields, sources)` (pure, `application/catalog.py`) maps the hit:
- name, brand and size from OFF or PLU;
- category from `domain/rules/catalog_categories.py`, or null when unmapped;
- tags: `organic` and leaf category names.

On a miss, nothing is added: the product keeps its printed-text name and null fields.

**(b) OFF tidying, only for OFF hits that need it.** A hit needs it when its name is all caps, its size could not be parsed, or its category is unmapped. It gets one `catalog_normalize/v1` request containing only that record (`name`, `brands`, `quantity`, `product_quantity`, `categories_tags`). The answer gives one value per field plus a **quote**: the exact substring of the record it came from. Code checks each value:
- **name** must equal the record's name ignoring case, accents and whitespace. Only casing may change ("CHEERIOS HONEY NUT" -> "Cheerios Honey Nut"). **No translation** (D-15).
- **brand:** the same rule, against `brands`.
- **size:** the quote must be a substring of `quantity`, and the value and unit must parse from it with `parse_size`. A count range such as `31/40` (shrimp per lb) is not a size: it becomes the tag `31/40 count`, and the size stays empty.
- **category** must be one of the 22 values, and its quote must be one of the record's `categories_tags` or a word of its name.
- A value that fails its check is dropped: that field stays as code mapped it, or empty, and the drop is listed in a NOTE. A value that passes is stored with source `off_llm` (confirmed).
- A record that needs no tidying makes **no request**. `GRAPH__MIN_CONFIDENCE` applies, as for the other prompts.

**What the agent saves.** Each product goes into `state["enrichments"]` as `{"product_id", "fields", "sources"}`, and `commit_enrichment` calls `describe_from_catalog` for each. A person's category memory still wins (source `person`).

**Trace:**
- TOOL rows for each lookup;
- an LLM row `catalog_normalize/v1` when tidying ran;
- a NOTE such as "described 2 from off (1 tidied), 1 from plu; 3 unknown: printed text kept", or "off 0060383064877: name casing ok, size dropped (no quote), category pantry from en:sauces".

The `enrichment/v1` prompt stays on disk and is no longer loaded.

**Category map** (`domain/rules/catalog_categories.py`, pure): an ordered rule list from most specific to least, and the first match wins.

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

Defaults by dataset: `beauty` -> personal_care, `petfood` -> pet. `products` needs its tags (`en:cleaning` -> household). A PLU -> produce. The table is data in code, so the test-author pins it as a table.

### 4.3 Learning

- **`items correct`** (`CorrectLineItem`): after the relink, if `parse_item_code(line.raw_text, store_key)` has a scheme, the code is upserted to the target product with source `correction` and the **person write policy** (it replaces a wrong mapping), in the same unit of work. It is logged `catalog.learned`.
  - `--new-product NAME` stamps `canonical_name` as `person`; `--category` stamps `category` as `person`.
- **Review answers** (`RecordReceipt` `_linked` with `product_names`): the same, with source `review`. Cuttable.
- Corrections made before this feature are picked up by the backfill (5.5).

### 4.4 Cross-store matching (comparison groups)

**Purpose.** Relate the same thing bought at different stores, for price comparison. A group is the set of products, from any stores, that a person or the model said are the same thing. Price comparison reads the regular and paid prices (discounts plan) of every product in a group. Products are never merged across stores.

**When it runs:**
- in the graph: a new **`link_across`** node (matching role) runs after `commit_enrichment`, for each product of this receipt that is **confirmed**; the supervisor's new **`commit_links`** node writes its decisions with `save_group_decisions`. The graph gains these two nodes, and its snapshot is updated;
- when `catalog add`, `fill` or `import`, or `describe_from_catalog`, confirms a product: the same use case runs (`--no-link` turns it off);
- in bulk: `catalog link-stores [--store S] [--apply]`, a dry run by default (cuttable).

**Flow for one confirmed product P, sold at the stores S_P:**
1. Embed P's confirmed name, brand and size. Only confirmed products have vectors.
2. Search: `product_similarity_search(text, k=3, exclude_stores=S_P, confirmed_only=true)`.
3. Drop a candidate C when any of these holds:
   - a stored decision for (P, C) exists;
   - a `person` decision contradicts the link (P or C was said "different" from a member of the other's group);
   - both brands are known and differ (compared after `normalize_alias`);
   - both sizes are known in comparable units (g/kg, ml/l, oz/lb converted) and differ by more than `MATCHING__SIZE_TOLERANCE` (0.05, i.e. 5 %).
4. **Automatic link:** the nearest remaining C with distance ≤ `MATCHING__AUTO_LINK_MAX_DISTANCE` (0.10) **and** both sizes known and equal within the tolerance -> **same**, source `vector`.
5. **Model confirmation:** otherwise, the candidates with distance ≤ `MATCHING__REVIEW_MAX_DISTANCE` (0.30) go into **one** `matching/v2` request. It shows P and each candidate as confirmed fields only (name, brand, size, category, source, store). The answer is `{decision: same|different, product_id, confidence, reason}`.
   - A "same" for an offered id with confidence ≥ `GRAPH__MIN_CONFIDENCE` -> **same**, source `llm`.
   - Anything else -> **different**, also stored, so the pair is not asked again.
   - An answer that never validates counts as "different" and is not asked again.
6. **Grouping:** "same" puts both products in one group, either an existing group or a new one. If both are already in different groups, nothing is merged: the pair is reported for a person.

**Trace:**
- TOOL rows for the search;
- an LLM row `matching/v2`;
- one NOTE per product, e.g. "cross-store #31 Gold Kiwi 1 kg (costco) ~ #57 Zespri SunGold Kiwi 1 kg (superstore): d 0.12 · llm same 0.86 · grouped", or "... d 0.08 · size 1 kg vs 2 lb · no auto-link -> llm different 0.91".

**A person decides with:**
- `catalog same P1 P2` and `catalog different P1 P2`, which record `person` decisions. These always win and are never asked again;
- `catalog groups [--product ID]`, which lists the groups.

### 4.5 Embeddings

- Only confirmed products have vectors. They are used for cross-store matching (4.4) and, later, for search.
- Printed-text products have none and are never candidates.
- `record_matches` (PLU/OFF-named new products), `describe_from_catalog` and the `catalog add/fill/import` writes embed a product when it becomes confirmed. The backfill deletes the vectors of unconfirmed products.

---

## 5. Store catalog CLI, assisted manual entry and existing data

**Conventions:**
- All commands accept `--run NAME` (that run's database, as elsewhere) and print one table.
- Store arguments go through `normalize_store_name`, so `costco` and `"Costco Wholesale"` are the same store.
- `CODE` is parsed with that store's profile: a GTIN or PLU code becomes a global entry, a store code a store entry.

```
paper-trail catalog todo [--store S] [--limit N] [--json]
        # unconfirmed products, most lines first:
        # code | printed name | times bought | last paid | last date | lookup link
        # lookup link = lookup_url(store, code), e.g. https://www.costco.ca/CatalogSearch?keyword=3139415;
        # printed, never requested; stores without a template show none
paper-trail catalog add STORE CODE ("NAME" | --from-title "TITLE" | --product ID)
                        [--brand B] [--size "1.36 kg"] [--category C] [--replace] [--yes] [--no-link]
        # fields stamped `person`; the code is attached (person policy). If the code already names
        # a printed-text product, that product is filled in place (no new product), so every past
        # line already points at it and every future line links by code. --replace is needed only
        # when a person already named that product. --from-title: 5.2. --product: the code names an
        # existing product (= adopt).
paper-trail catalog fill [--store S]
        # one product at a time in todo order: shows printed text, code, times bought, last paid and
        # the lookup link; asks name (or "t" to paste a title, 5.2), brand, size, category (list
        # shown; Enter = leave empty); "s" skips, "q" stops. Prefills nothing.
paper-trail catalog export --todo [--store S] > costco.csv
        # store,code,printed,times,last_paid,lookup_url,name,brand,size_value,size_unit,category
paper-trail catalog import FILE [--apply] [--replace]       # lookup_url ignored; empty cells stay empty
paper-trail catalog list [--store S] [--scheme gtin|plu|store] [--json]
paper-trail catalog remove --store S --code C              # the product stays
paper-trail catalog adopt --store S --code C --product ID [--move-lines]
        # the code now names product ID; --move-lines relinks every line of the code's printed-text
        # product to ID (price observations move with their discount fields) and deletes the
        # printed-text product when no line is left; refused for lines a person linked (D-12)
paper-trail catalog adopt --store S --all-matched [--apply]
        # cuttable: proposes every (store, code) whose ITEM lines all link to ONE product;
        # conflicts listed, never applied; dry run by default
paper-trail catalog same P1 P2 | different P1 P2 | groups [--product ID]   # 4.4
paper-trail catalog link-stores [--store S] [--apply]                     # 4.4, cuttable
paper-trail catalog lookup "<raw line text>" --store S                    # debug: parse + which source hits
paper-trail catalog build [--force] [--keep-download] | plu-import FILE | status
paper-trail catalog backfill [--apply]
```

**Samples:** only `docs/samples/store-catalog.sample.csv` (placeholder `SAMPLE ITEM` rows) is committed. Real catalogs stay in the data volume (ADR-0015).

### 5.1 Costco: assisted manual entry, fast for a household

1. **The work comes in order.** `catalog todo --store costco` sorts by times bought, so the 20 things bought every trip come first. Today's 9 distinct Costco item numbers take minutes.
2. **One click per item.** Each row carries its costco.ca search link. Open it in the browser, check the page against the printed text, then either type `catalog add costco 3139415 "Gold Kiwi" --size "1 kg" --category produce` or paste the title with `--from-title`.
3. **When the search finds nothing** (warehouse-only, or a different number), name the item from the shelf tag or the receipt text, or leave it as printed. That is still correct.
4. **Once per item, ever.** Every line with that (store, code) points at one product. Filling it renames the product for every past line at once, and future lines link by the code.
5. **Two styles:** `todo` + `add` (or `fill`) for a few items after each trip; `export --todo` -> spreadsheet -> `import --apply` for a first catch-up session.
6. **No typing for what a source knows:** Superstore GTINs hit OFF, and produce PLUs hit `plu.csv`.

### 5.2 Title parsing (`--from-title`)

```
paper-trail catalog add costco 3139415 --from-title "Zespri SunGold Kiwi, 2 lb" [--yes]
```

1. **Request.** One `catalog_title/v1` request. Its input is the title, plus the store and the printed text (`GOLD KIWI`) as context. The answer gives name, brand, size_value, size_unit and category, with a **quote** from the title for each field.
2. **Checks in code:**
   - name, brand and size must each quote a substring of the title (case-insensitive);
   - the size must parse with `parse_size` from its quote;
   - category must be one of the 22 values. It is the only field allowed to be an inference, and it is shown as "suggested";
   - a field that fails its check is left empty.
3. **Confirmation.** The CLI shows each field with its quote and asks "save? [y/N/e(dit)]". With "e", each field can be retyped.
4. **Saving.** On save, every field is stamped `person` (the person confirmed it). `field_sources` notes that the value was parsed from a title, for the trace and for audit.
5. **`--yes`** skips the confirmation; its help says the owner then vouches for the parse. The default is to confirm.
6. **Privacy.** The title is masked like other free text and is not stored as a whole. **The app fetches nothing**: the owner pastes the title from their own browser.

### 5.3 Later: web page

A "Products to name" page (PLAN stage 6) with the same `todo` order, the lookup link and an inline form per row, plus a "paste title" field. It calls the same `CatalogFill`/`CatalogAdd` use cases, so the CLI and the page share one write path and one provenance rule. It is not in this plan's estimate.

### 5.4 Use cases (`application/catalog.py`)

`CatalogTodo`, `CatalogAdd` (with `from_title`), `CatalogFill`, `CatalogAdopt(move_lines)`, `CatalogImport`/`Export`, `CatalogBackfill`, `LinkAcrossStores` (4.4), `RecordGroupDecision`, `catalog_fields(entry)`, `parse_title(...)` (the prompt call and the quote checks).

### 5.5 `catalog backfill` (existing data)

A dry run by default and idempotent. It writes one unit of work per product and re-embeds outside the transaction. For each product with linked ITEM lines:

1. **Codes.** Parse each line's code with its receipt's store.
2. **Split.** Lines whose codes differ were joined by alias, vector or LLM matching.
   - The product keeps its most frequent code.
   - The lines of every other code move to that code's product: an existing one in `product_codes`, else a new printed-text product. The move uses `link_items` + `prices.reassign`, which keeps the discount fields; no correction record is written.
   - Uncoded lines stay.
   - Lines a **person** linked (`match_source = memory`) are never moved; a conflict is reported instead.
3. **Attach** the code (source `backfill`, machine policy).
4. **Reset** every field whose source is `legacy_model`:
   - name -> `printed_name` of the product's first line (`receipt_text`);
   - brand, size and tags -> empty;
   - category -> null.
5. **Kept and stamped `person`:**
   - the name of a product a person created: either the PRODUCT_LINK correction that created it has the same `created_at` as the product (`items correct --new-product`), or a name alias with source IMPORT exists (an import, or a review's `product_names`);
   - the category from the newest CATEGORY correction for the product's alias and store.
6. **Data sources.** PLU/OFF lookup of the code. A hit fills the fields a person did not keep, with their sources. OFF tidying (4.2b) runs only with `--tidy`, which needs the LLM host.
7. **Vectors.** Delete the vectors of unconfirmed products and re-embed confirmed ones. Cross-store linking is not run here; use `catalog link-stores`, or let `add`/`fill` link as products get named.
8. **Aliases.** Re-point MATCHED aliases of moved lines to the line's new product. Person aliases are never touched. Re-normalizing the tax flags is cuttable.
9. **Report.** A before/after table per product: code | lines | name, brand and category before -> after, with their sources. Then the splits, and merge candidates within a store (two products with the same code). A second `--apply` reports "0 changes".

---

## 6. Settings

`PAPER_TRAIL_CATALOG__*` (new `CatalogSettings`):

| Variable | Default | Why |
|---|---|---|
| `CATALOG__ENABLED` | `true` | `false` skips step 1 of matching (A/B arm): coded lines then go the uncoded way, still with no model |
| `CATALOG__OFF_PATH` | `<data_dir>/catalog/off.sqlite` | 1.2 |
| `CATALOG__PLU_PATH` | `<data_dir>/catalog/plu.csv` | 1.4 |
| `CATALOG__COUNTRY` | `en:canada` | subset filter |
| `CATALOG__EXPORTS` | the food, beauty, products and pet food CSV URLs | 1.1 |
| `CATALOG__MIN_ROWS` | `20000` | build sanity floor |
| `CATALOG__ONLINE_FALLBACK` | **`false`** | 1.5, ADR-0018 |
| `CATALOG__ONLINE_URL` / `__ONLINE_TIMEOUT_S` | `https://world.openfoodfacts.org` / `5` | 1.5 |

`PAPER_TRAIL_MATCHING__*`, now cross-store only (4.4):

| Variable | Default | Why |
|---|---|---|
| `MATCHING__CROSS_STORE` | `true` | `false` skips `link_across` |
| `MATCHING__AUTO_LINK_MAX_DISTANCE` | **0.10** (was 0.15) | automatic link only with equal sizes (D-14) |
| `MATCHING__REVIEW_MAX_DISTANCE` | 0.30 | model-confirmed band |
| `MATCHING__SIZE_TOLERANCE` | 0.05 | size check |

**Prompt versions:**
- `PROMPTS__MATCHING_VERSION` becomes `v2`;
- new `PROMPTS__CATALOG_NORMALIZE_VERSION` (`v1`) and `PROMPTS__CATALOG_TITLE_VERSION` (`v1`);
- `PROMPTS__ENRICHMENT_VERSION` is unused, documented as such and removed in a later clean-up.

**Other rules:**
- The store code profiles, `STORE_SEARCH_URLS` and the PLU ranges are module constants, not settings.
- `process --no-catalog` sets `CATALOG__ENABLED=false`. Like the other configuration flags, it is refused on `--resume`.

---

## 7. ADR-0018 and amendments

**ADR-0018 "Products are identified by the code on the receipt; descriptions come only from data"** decides:
1. **Identity within a store is the code** ((store, code), or a GTIN/PLU), matched exactly. **Products are related across stores** in comparison groups, by embeddings and the model's confirmation, between confirmed products only. No model ever names an unknown product.
2. **Every product field carries its source** (`receipt_text`, `person`, `plu`, `off`, `off_llm`). Category is null when no source has one.
3. Codes live in `product_codes`, with a machine/person write policy; there is no `products.barcode`.
4. OFF is used offline, as a separate read-only file built by an explicit owner command from the CSV export.
5. An online fallback that is barcode-only and default off.
6. No IFPS data in the repo. No Costco data is collected by the app: Costco items are entered by a person, helped by a printed costco.ca search link that the person opens. The app makes no request to costco.ca or to fan sites (section 1.6).
7. ODbL attribution.
8. **The model only reads data in front of it.** Title parsing and OFF tidying must quote their source for every value, and code checks each quote.

**Context:** the 40 % wrong identities.

**Alternatives considered:**
- the model with better prompts (it cannot know that `CM` is a tortilla brand);
- model suggestions marked unverified (D-6);
- vector matching within a store with a tighter threshold;
- the costco-mcp internal API, and scraping costco.ca (both against the terms);
- the full OFF dump, or the API per receipt;
- Parquet with DuckDB;
- commercial APIs.

**Consequences:**
- unknown products read as printed until a person names them;
- within-store matching calls no model;
- the model's remaining roles are listed in section 0.1;
- earlier experiment results for within-store vector and LLM matching describe a retired path.

**Amendments:**
- **ADR-0004:** a second, read-only SQLite file for public reference data.
- **ADR-0009:** three new structured-output prompts (`matching/v2`, `catalog_normalize/v1`, `catalog_title/v1`).
- **ADR-0013:** `catalog_lookup` (matching, enrichment); `describe_from_catalog` and `save_group_decisions` (supervisor); the new search inputs; `save_enrichment` removed.
- **ADR-0015:**
  - `catalog build` downloads public exports and sends no data;
  - the optional barcode-only fallback;
  - a printed lookup URL is not a request: the app never contacts store websites;
  - attribution text.
- **ADR-0017:** check digits computed in code; `printed_name`.
- **PLAN 6:** the matching order.

**ODbL obligations** (Open Food Facts database ODbL, contents DbCL; images CC-BY-SA, which we do not use):
- **(a) Attribution.** The notice is "Contains information from Open Food Facts, made available under the Open Database License (ODbL)". It goes in the README, in `catalog status`, in `meta`, and in any report or screenshot that shows catalog-derived product data. The course report is a "Produced Work", so it needs the notice.
- **(b) Share-alike.** The subset is a Derivative Database. It is never committed or distributed; each install builds its own, so no share-alike obligation arises. If it were ever published, it would have to be offered under the ODbL.
- **(c) No DRM** or technical restrictions on anything shared.

---

## 8. Acceptance criteria

- **BC-AC-01** `gs1_check_digit` / `is_valid_gtin`:
  - `03400047069` -> `3`; `06038306487` -> `7`; `05889165231` -> `0`;
  - valid EAN-8, EAN-13 and GTIN-14 codes are accepted;
  - a wrong check digit is rejected.
- **BC-AC-02** `off_key`:
  - `034000470693` -> `0034000470693`;
  - 13 digits unchanged;
  - codes of 7 digits or fewer are padded to 8;
  - leading zeros beyond 13 digits are stripped.
- **BC-AC-03** `parse_item_code` (table-driven):
  - `(1)05700001285 CRV ...` -> GTIN, key `0057000012854`;
  - `06038306487 PC UNS BABYWIPES HMRJ` -> GTIN `0060383064877`;
  - `4046 AVOCADO SMALL` -> PLU `4046`;
  - `94046 ...` -> PLU `4046`, organic;
  - `62021 APPLES` -> STORE_ITEM;
  - `2100580 LN GRND BF & PK` -> IN_STORE;
  - an 11-digit code starting with `2` or `4` -> IN_STORE, starting with `5` -> NONE;
  - at store `costco`: `1793017`, `18584` and `1281` -> STORE_ITEM;
  - `12 EGGS` and `2% MILK` -> None;
  - the 12- and 13-digit cases of section 2;
  - the existing ADR-0017 split cases are unchanged.
- **BC-AC-04** Category map:
  - the pinned table of tags -> category;
  - the first rule wins;
  - unknown tags -> None;
  - the dataset defaults;
  - PLU -> produce.
- **BC-AC-05** `catalog_fields` and `printed_name`:
  - `catalog_fields` builds fields **and their sources** from an OFF or PLU entry: size from `product_quantity`/unit and from `quantity` text; unparseable -> no size; brand kept separate; organic PLU -> "Organic ..." plus the tag; unmapped category -> null;
  - `printed_name`: `06038306487 PC UNS BABYWIPES HMRJ` -> `PC UNS BABYWIPES`; `3139415 GOLD KIWI` -> `GOLD KIWI`; `(1)05700001285 CRV CHKKN PBLN BR MRJ` -> `CRV CHKKN PBLN BR`; `TH 2PC PJ` unchanged; never empty.
- **BC-AC-06** Migration 0005 (`down_revision` 0004):
  - upgrade and downgrade round-trip;
  - `product_codes` constraints: UNIQUE, the scheme/store CHECK, cascade on product delete;
  - the existing `match_source` values survive the column swap, and `catalog` is accepted;
  - `products.field_sources` is added, and existing products are stamped `legacy_model` for each non-null field;
  - `product_groups`, `products.group_id` and `product_group_decisions` exist with their constraints (ordered pair, UNIQUE, cascade, SET NULL);
  - the `line_items` and `products` FTS triggers still fire;
  - the schema-matches-metadata test passes;
  - migrations 0001-0004 are unchanged.
- **BC-AC-07** Repository contracts:
  - `ProductCodeRepository`: find, upsert, list, remove; the machine policy keeps an existing row and logs the conflict; the person policy replaces it;
  - `ProductGroupRepository`: decision lookup in either order; `join` creates or reuses a group; two different groups are refused.
- **BC-AC-08** Adapters:
  - `OffCatalog` over a fixture file: hits in key order; a missing file -> miss plus one warning; read-only (a write attempt fails); reopens after the file is replaced;
  - `PluCatalog`: hits, organic, a missing file -> miss;
  - `CompositeCatalog`: the order holds (a store catalog row shadows OFF).
- **BC-AC-09** `catalog build` against a **local fixture HTTP server** serving tiny gzip CSVs (tabs, a malformed row, a non-Canadian row, a row without a name):
  - only Canadian rows with a name are kept;
  - `meta` is filled;
  - the replace is atomic: a failure mid-build leaves the old file intact;
  - the row floor refuses a short build;
  - a 304/ETag skip;
  - Range resume;
  - no request goes anywhere except the configured URLs.
- **BC-AC-10** Tools:
  - `catalog_lookup` is allowed for matching and enrichment and denied for the others (exit 5 in `tools call`); it parses `raw_text`, and its output validates;
  - `describe_from_catalog` and `save_group_decisions` are supervisor-only. `describe_from_catalog` writes only fields with a source, never replaces a `person` field, and embeds only when the product becomes confirmed. `save_group_decisions` is idempotent;
  - `product_similarity_search` with `confirmed_only` and `exclude_stores` filters as specified;
  - `save_enrichment` is listed for no role;
  - in-process and MCP results are identical.
- **BC-AC-11** Matching within a store (fake catalog; the ScriptedLlm asserts **zero** requests from `match`):
  - a known code -> `catalog`, with no `product_lookup`, similarity search or model call;
  - an OFF or PLU hit -> NEW named from the entry, with code and entry in the decision;
  - an unknown code -> NEW named `printed_name`, with the code at source `receipt`;
  - 5 lines with one code -> one product;
  - an uncoded line -> correction or alias, else NEW with the printed text, and no similarity search;
  - DISCOUNT lines and person-named lines are never looked up;
  - `CATALOG__ENABLED=false` -> no `catalog_lookup` call, and still no model call.
- **BC-AC-12** `save_matches` / `record_matches`:
  - codes are inserted in the same transaction, idempotently on resume;
  - an alias link attaches no code;
  - a conflicting code is not stolen;
  - no embedding call for printed-text products; one per OFF- or PLU-named product.
- **BC-AC-13** Lookup/Enrichment agent:
  - a clean OFF hit -> one `catalog_lookup` and one `describe_from_catalog` with sources recorded, and **0 model requests**;
  - a miss -> no write: the printed name and a null category stay;
  - an ALL-CAPS name -> exactly one `catalog_normalize/v1` request, and the saved name differs from the record only in casing (`off_llm`);
  - a translated name or an invented brand in the answer is dropped and listed in a NOTE;
  - `31/40` -> the tag `31/40 count`, no size;
  - an unmapped category with a quote from `categories_tags` -> saved as `off_llm`; without a quote -> null;
  - a person's category correction wins (`person`);
  - in-process and MCP transports give identical results.
- **BC-AC-14** Learning:
  - `items correct` on a Costco line upserts `("costco", "1793017")` to the target, replacing a previous mapping, and the next receipt with that line links via `catalog`;
  - `--new-product` and `--category` stamp `person`;
  - a line without a code learns nothing;
  - the review `product_names` case is the same (cuttable).
- **BC-AC-15** CLI:
  - **`todo`:** ordered by times bought descending, then last date; every Costco row shows `https://www.costco.ca/CatalogSearch?keyword=<code>`; other stores show no link; `--json` carries `lookup_url`.
  - **`catalog add costco 3139415 "Gold Kiwi" --size "1 kg" --category produce`** on a code that names a printed-text product fills that product in place (fields `person`, no new product). Every past line of that code shows the new name through its product, its price observations keep their discount fields, and the next receipt with that code links via `catalog` with no model call. On a code that no product has yet, it creates the product. It is refused without `--replace` when a person already named the product.
  - **`fill`** (scripted input): shows the link, stamps `person`, supports skip and quit, leaves empty answers empty, prefills nothing.
  - **CSV:** `export --todo` -> `import --apply` round-trips; `lookup_url` is ignored on import; the `import` dry run writes nothing.
  - **`adopt --move-lines`:** relinks lines and observations (discount fields kept), deletes the emptied printed-text product, and is refused for lines a person linked.
  - **`same`/`different`/`groups`:** record and list person decisions.
- **BC-AC-16** `catalog backfill` on a fixture database:
  - a product whose lines carry two codes is split, and its observations move with it;
  - `legacy_model` fields are reset to the printed text or null;
  - a name a person created and a CATEGORY correction are kept (`person`);
  - PLU/OFF hits are applied with their sources;
  - vectors of unconfirmed products are deleted;
  - memory-linked lines are never moved (the conflict is reported);
  - merge candidates are reported;
  - the dry run writes nothing, and a second `--apply` -> 0 changes.
- **BC-AC-17** Data-local:
  - under a socket guard that fails on any connection, `process` with the default settings (fake LLM) and `catalog todo/add <name>/fill/export` attempt no connection;
  - with `--from-title` (scripted LLM adapter), only the configured Ollama URL is allowed;
  - a static test asserts that `costco.ca` appears in the source only inside `STORE_SEARCH_URLS`, and `cocowest` only in documentation;
  - with `ONLINE_FALLBACK=true`, the fake HTTP server sees only `/api/v2/product/<gtin>` with no other receipt data, and a timeout counts as a miss.
- **BC-AC-18** Settings: the defaults of section 6; `--no-catalog` is refused on `--resume`.
- **BC-AC-19** (cuttable) Alias re-normalization: the backfill re-normalizes stored aliases and corrections for `mrj`/`hmrj`, idempotently, merging duplicates (newest wins).
- **BC-AC-20** `lookup_url` (table-driven):
  - `("costco", "3139415")` -> `https://www.costco.ca/CatalogSearch?keyword=3139415`;
  - leading zeros are kept as printed;
  - a non-digit code is URL-quoted;
  - `("real canadian superstore", ...)` -> None.
- **BC-AC-21** Cross-store (fake embeddings, scripted LLM):
  - unconfirmed products and products from the same store are never candidates;
  - d ≤ 0.10 with equal sizes -> grouped `vector`, with no model call;
  - d ≤ 0.10 with a size conflict -> no automatic link; the model is asked;
  - 0.10 < d ≤ 0.30 -> exactly one `matching/v2` request; "same" at or above the minimum confidence -> grouped `llm`; otherwise `different` is stored and the pair is not asked again;
  - d > 0.30 -> nothing;
  - different known brands -> skipped;
  - a `person` "different" is never overridden, and `catalog same` groups at once;
  - two existing groups are reported, not merged;
  - every decision has a NOTE with distance, source and confidence;
  - resuming after a crash in `commit_links` changes nothing.
- **BC-AC-22** Title parsing:
  - "Zespri SunGold Kiwi, 2 lb" -> name, brand "Zespri" and size 2 lb, each with its quote, saved after confirmation;
  - an answer whose name or brand is not in the title -> that field is empty;
  - "N" saves nothing;
  - saved fields are `person`;
  - `--yes` saves without a prompt.
- **BC-AC-23** `make ci` green, coverage of at least 90 %:
  - the OpenAPI spec and TS client are regenerated (`match_source: catalog`, `field_sources`, `group_id`);
  - the graph snapshot is updated for `link_across` and `commit_links` only;
  - the tool role snapshots are updated;
  - prompt-file tests pass for `matching/v2`, `catalog_normalize/v1` and `catalog_title/v1`: schema.json equals the Pydantic schema, and the examples use only `SAMPLE` content.

## 9. Test list for the test-author

**Fixture rules:**
- no real network, no real OFF, no real IFPS data;
- no receipt codes from the owner's database, except the codes already quoted in this plan;
- fixtures use OFF's documented example `034000470693`, synthetic GTINs with valid check digits, and `SAMPLE` names.

| Suite | File |
|---|---|
| unit/domain | `test_item_codes.py` (AC-01..03), `test_catalog_categories.py` (AC-04), `test_printed_name.py` + `test_catalog_fields.py` (AC-05), `test_lookup_url.py` (AC-20), `test_product_confirmed.py` |
| integration | `test_migration_0005.py` (AC-06), `test_product_codes_sqlite.py` + `test_product_groups_sqlite.py` (AC-07), `test_off_catalog.py` + `test_plu_catalog.py` (AC-08, fixture files built in `tmp_path`), `test_catalog_build.py` (AC-09, `http.server` on loopback in a thread), `test_catalog_backfill_sqlite.py` (AC-16, 19) |
| contract | `test_product_catalog_contract.py`: the same cases against `FakeCatalog`, `CompositeCatalog` (fakes) and the SQLite adapters |
| unit/tools | `test_catalog_lookup_tool.py`, `test_describe_from_catalog_tool.py`, `test_save_group_decisions_tool.py`, `test_similarity_search_filters.py` (AC-10); registry allowlist table update |
| unit/agents | `test_matching_codes.py` (AC-11, asserts 0 model requests), `test_enrichment_catalog_agent.py` + `test_catalog_normalize.py` (AC-13), `test_cross_store_linking.py` (AC-21), graph run through `agent_harness` (AC-12). The existing within-store vector/LLM-band matching tests and the LLM enrichment tests are **removed or rewritten** to the new rule; each one is listed in `docs/tests/barcode-lookup.md` |
| unit/application | `test_correction_learns_code.py` (AC-14), `test_catalog_fill.py`, `test_catalog_title_parse.py` (AC-22, scripted LLM) |
| unit/interfaces | `test_cli_catalog.py` (AC-15, scripted stdin), `test_cli_catalog_csv.py` |
| unit | `test_config_barcode.py` (AC-18), `test_catalog_privacy.py` (AC-17, socket guard and static scan) |
| prompts | `test_prompts_catalog.py` (AC-23 prompt files) |
| data | `tests/data/catalog/off_fixture.csv` (about 10 rows, including one ALL-CAPS, one `31/40`, one unmapped category), `plu_fixture.csv`, `store-catalog.sample.csv` |

Pin these in `docs/tests/barcode-lookup.md`, as the other plans do.

## 10. Experiment E-BC (after `make catalog`)

1. Run `make catalog`. Record the download size, build time, rows kept and file size, which bear out the 130 k / 25-40 MB estimates.
2. Run `make pt ARGS="catalog backfill"` (dry run, main database). Record the before/after table and the splits for the existing products, and the hit counts. Expected eligible codes on the first three receipts:

| Receipt | Item lines | GTIN (OFF) | PLU range | Store's own 4-5 digit | In-store 7-digit | Store item (Costco) |
|---|---|---|---|---|---|---|
| Superstore 1 | 18 | 10 | 5 (3121, 3156, 4029, 4048, 4053) | 1 (62021) | 2 (2100580, 2348420) | - |
| Superstore 2 | 8 | 6 | 2 (4046, 4068) | - | - | - |
| Costco | 13 (9 distinct) | - | - | - | - | 13 (named by hand) |
| **Total** | 39 | **16 lookup-able in OFF** | **7, hit only with `plu.csv` or curated** | 1 | 2 | 13 |

   The actual OFF hit rate (of 16) is reported here. Coverage of Loblaw's private label (`060383...`) in OFF is the open question; whatever OFF misses, `catalog add` covers (it works for GTINs too).
3. Run `--apply`, then reprocess the three receipts in a run, with and without `--no-catalog`. Compare:
   - match sources;
   - **model calls in `match` (expected 0 in both arms)**;
   - `catalog_normalize` requests;
   - time;
   - **identity errors judged by the owner: before (about 40 %) and after (expected 0 wrong identities; unknown items read as printed)**;
   - the count of printed-text products left to name.
4. Costco: run `catalog todo --store costco`, open each link, and name the 9 item numbers with `catalog add costco <code> "<name>"` or `--from-title`. Record:
   - how many codes the costco.ca search found (a check on "often but not always");
   - which items were named from the shelf tag or the receipt text instead;
   - how many title parses needed an edit.

   Then reprocess. Expected:
   - 13/13 item lines `catalog`;
   - no model call in `match`;
   - Gold Kiwi named as entered, on all historical lines.
5. Cross-store: after the Costco names and the Superstore OFF hits, record each cross-store pair (distance, `vector` or `llm`, confidence) and the owner's judgement of each grouping (right or wrong). Use these to recalibrate D-14.

After this run, update the README "What gets saved" example: product 24's name and field sources, its match sources, its group, and the regular/paid fields from the discounts plan. Every screenshot or table showing OFF data carries the ODbL line.

## 11. Files

| File | Change |
|---|---|
| `domain/rules/item_codes.py` | new: codes, check digits, `printed_name`, `lookup_url`, `STORE_SEARCH_URLS` |
| `domain/rules/catalog_categories.py` | new (4.2) |
| `domain/rules/normalization.py` | tax flags `mrj`, `hmrj` |
| `domain/models.py`, `domain/enums.py` | `ProductCode`, `ProductGroupDecision`, `Product.field_sources`/`group_id`, `is_confirmed`; `MatchSource.CATALOG`, `CodeScheme`, `CodeKind`, `FieldSource` |
| `ports/catalog.py`, `ports/repositories.py`, `ports/unit_of_work.py` | `ProductCatalog`, `CatalogProduct`; `ProductCodeRepository`, `ProductGroupRepository` |
| `adapters/sqlite/{schema,mappers,product_code_repository,product_group_repository,store_catalog,vector_repository,unit_of_work}.py` | tables, repositories, store catalog, search filters |
| `adapters/catalog/{off,plu,composite,off_online,build}.py` | adapters and the build pipeline |
| `migrations/versions/0005_product_codes.py` | section 3.2 |
| `application/catalog.py` | todo, add (incl. title), fill, adopt (move lines), import/export, backfill, `catalog_fields`, cross-store linking, group decisions |
| `application/enrichment.py` | retired; the CATEGORY-memory rule moves to `catalog.py` |
| `application/matching.py`, `application/corrections.py`, `application/receipts.py` | codes in `plan_matches`/`record_matches`; learning hooks; embeddings only for confirmed products |
| `tools/{models,handlers,registry}.py` | `catalog_lookup`, `describe_from_catalog`, `save_group_decisions`, search filters; `save_enrichment` removed; `MatchDecisionIn.code`/`.catalog` |
| `agents/nodes/{matching,enrichment}.py`, new `agents/nodes/cross_store.py`, `agents/nodes/commit.py`, `agents/graph.py` | sections 4.1, 4.2, 4.4; `link_across`, `commit_links` |
| `agents/prompts/matching/v2/**`, `catalog_normalize/v1/**`, `catalog_title/v1/**` | new prompts (`SAMPLE` examples only) |
| `config.py`, `bootstrap/` | `CatalogSettings`, `MatchingSettings` changes, prompt versions, wiring |
| `interfaces/cli/catalog.py`, `main.py`, `process.py` | the `catalog` sub-app, `--no-catalog` |
| `interfaces/web/schemas/*` | `field_sources`, `group_id` |
| `Makefile` | `catalog`, `plu` targets |
| `docs/adr/0018-...md`; ADR-0004, 0009, 0013, 0015, 0017 amendments; README (attribution, `make catalog`, matching section rewritten, the Costco todo -> link -> add workflow and its limits); `docs/PLAN.md` deltas; `docs/tests/barcode-lookup.md` | |

## 12. Effort and cut line

| Piece | Estimate |
|---|---|
| Domain codes, check digits, category map, `printed_name`, `lookup_url`, `catalog_fields` + tests | 1.5 h |
| Migration 0005 (`product_codes`, `match_source` swap, `field_sources`, groups), repositories, enums, OpenAPI regen | 1.75 h |
| `catalog build` (stream filter, meta, atomic, resume) + `make catalog` | 1.5 h |
| Port, OFF/PLU/store/composite adapters, fake, `catalog_lookup` tool | 1 h |
| Matching within a store (codes, printed text, no model), `save_matches` codes, embeddings only for confirmed | 1.5 h |
| Lookup/Enrichment agent (lookup part), `describe_from_catalog`, provenance | 1 h |
| OFF tidying (`catalog_normalize/v1`, quote checks, `off_llm`) | 1.25 h |
| Cross-store matching (`link_across`, `commit_links`, `save_group_decisions`, `matching/v2`, search filters, `same`/`different`/`groups`) | 2.5 h |
| Manual entry CLI (`todo` with links, `add`, CSV export/import, `list`/`remove`, `adopt --move-lines`) + `items correct` learning + the no-network test | 2.5 h |
| `--from-title` (prompt, quote checks, confirmation) | 1 h |
| Backfill (codes, split, reset, sources, vectors, aliases) | 2 h |
| ADR-0018, amendments, README, section 0.1, experiment | 1.25 h |
| **Core subtotal** | **about 18.75 h** (an agent-pipeline estimate, including review) |
| Cuttable: online fallback 1 h, `adopt --all-matched` 0.4 h, interactive `fill` 0.5 h, bulk `link-stores` 0.5 h, alias re-normalization 0.3 h, review learning hook 0.3 h | about 3 h |
| **Total** | **about 21.75 h** |

**Cut order (first to go):**
1. Online fallback.
2. `adopt --all-matched`.
3. Interactive `fill` (`todo` + `add` and the CSV remain).
4. Bulk `link-stores` (linking still runs in the graph and on `add`).
5. Alias re-normalization.
6. Review learning hook.

**Minimum viable:**
- domain codes, `printed_name` and `lookup_url`;
- migration 0005;
- matching within a store without a model;
- the Lookup/Enrichment agent (lookup part);
- `catalog todo` with links, `add` (incl. `--from-title`), CSV export/import, `adopt --move-lines`;
- `items correct` learning;
- the backfill;
- cross-store `link_across`.

`make catalog` and OFF tidying can slip together to a second pass: without them, Superstore GTINs stay printed-text products until the build runs. That is still correct, only less complete.

**Dependency:** `docs/plans/discounts-and-paid-prices.md` is merged first. It provides migration 0004, the `(N)` marker, and the observation discount fields that split and adopt move.

## 13. Owner decisions

**Approved 2026-11-08: the recommended option for each.**

- **D-1** Approve `product_codes` (several codes per product) instead of `products.barcode`.
- **D-2** A product created for a coded line always takes that line's code, including Costco item numbers (source `receipt` in `catalog list`); there is no setting. Confirm.
- **D-3** The backfill resets every model-written field to the printed text or null, splits products whose lines carry different codes, applies PLU and OFF, and keeps every person choice.
- **D-4** PLU: import your own IFPS export (default), or ask IFPS for permission to commit a seed.
- **D-5** Keep the online OFF fallback in scope (default off), or drop it.
- **D-6** Category for a product no source knows: **null (recommended**, matching "enrich only with the database"), or a model suggestion stored in a separate column, marked unverified and never used in reports.
- **D-7** Lines without a code: a person's correction or an exact store alias only, else a new printed-text product (recommended).
- **D-8** Embeddings only for confirmed products, used for cross-store matching and search; never for identity within a store. `enrichment/v1` and `matching/v1` stay on disk, unused.
- **D-9** Display name of an unknown product: the printed text without the code and the observed tax flags, with case and abbreviations kept (recommended), or the raw text exactly as printed.
- **D-10** The model is used for: vision and text extraction, feedback retries, cross-store matching, title parsing and OFF tidying. Within-store matching, naming unknown products and discount attribution are code (section 0.1). Confirm this split for the course report.
- **D-11** Costco: assisted manual entry. `todo` prints a costco.ca search link per unknown code; the owner looks the item up in their own browser and names it with `catalog add STORE CODE "NAME"` or `--from-title`. No scraping, no internal-API tool, no automated fetch of costco.ca or fan sites.
- **D-12** No automatic merges within a store: the backfill reports merge candidates, and a person merges with `catalog adopt --move-lines`.
- **D-13** Cross-store links as comparison groups (`product_groups`), not merges (recommended).
- **D-14** Cross-store thresholds: automatic link at distance ≤ 0.10 only with equal sizes, model-confirmed up to 0.30, size tolerance 5 %. These are starting points, recalibrated in experiment E-BC step 5.
- **D-15** OFF tidying may change casing only, not translate a French name (recommended: a translation cannot be checked against the record).
- **D-16** `--from-title` asks for confirmation by default; `--yes` is allowed.
