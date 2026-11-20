# The command line

Everything the agents and the catalog do can also be run from a terminal, in Docker, through
`make process`, `make pt` or `docker compose run`. What the agents do with a receipt is in
[How it works](how-it-works.md).

## Importing receipts

Besides photos, a receipt can be recorded from a JSON file with the CLI, for example one typed
by hand when a photo cannot be read:

```sh
docker compose run --rm -v "$PWD/tests/data/receipts:/import:ro" web \
  receipts import /import/costco_simple.json            # add --dry-run to only check it
docker compose run --rm web receipts show 1             # the receipt as JSON
```

The import prints `receipt <id>: <status>` and every arithmetic issue. A receipt that does not
add up is still recorded, as `needs_review`, with an open review item; one that does is
`completed` and, when its purchase date is known, adds a price observation for each `item` line
that names a product (a deposit, discount or fee line gets a product only when the file names
one for it, and is never priced itself; a discount that names an item's code, such as
`TPD/1234567`, is applied to that item's paid price, see [What gets saved](how-it-works.md#what-gets-saved-a-product-and-its-price-history)). A discount
line's `applies_to` (the 0-based position of the item it reduces) sets that link by hand; the
rest are found from the codes. `--dry-run` also lists each discount and the item it applies to,
or why it stays on the receipt. Card numbers are masked before anything is stored or logged.
Invalid files exit with status 2 and name the offending field. The format (unknown fields are
errors, amounts are integer cents; the lines add up to the subtotal, and subtotal plus tax to
the total; `tests/data/receipts/costco_simple.json` is a full example):

```json
{
  "store_name": "COSTCO WHOLESALE #1234",
  "purchased_on": "2026-11-01",
  "currency": "CAD",
  "subtotal_cents": 158,
  "tax_cents": 52,
  "total_cents": 210,
  "payment_last4": "1111",
  "items": [
    {
      "raw_text": "KS 2% MILK 4L",
      "description": "Kirkland 2% Milk 4 L",
      "quantity": "2",
      "unit_price_cents": 329,
      "line_total_cents": 658,
      "product": "Kirkland 2% Milk 4 L",
      "category": "dairy_eggs"
    },
    { "raw_text": "TPD/1234567", "kind": "discount", "line_total_cents": -500 }
  ]
}
```

Only `store_name`, `total_cents`, `items`, and each item's `raw_text` and `line_total_cents` are
required. Defaults: `currency` `CAD`, `quantity` `"1"` (a decimal string), `kind` `item` (or
`discount`, `deposit`, `fee`), `description` the raw text, `unit` none (unknown, compared as
`each`). A weighed line sets `"unit": "kg"` or `"lb"`, with `quantity` the weight and
`unit_price_cents` the price per kg or lb; `unit` is `each`, `kg` or `lb`, anything else is
refused. `GET /api/categories` lists the category values.

## Processing receipts with the agents

The agents run from the command line in the on-demand `cli` service, which mounts the receipts
folder `PAPER_TRAIL_RECEIPTS_DIR` (default `./receipts`, git-ignored; set your own folder in `.env`)
read-only at `/receipts`. Set `ANTHROPIC_API_KEY` in `.env` first, then:

```sh
make build                                         # once, and after pulling changes
make process RECEIPTS=costco.jpg RUN_NAME=try1     # one photo, in its own run "try1"
make process RUN_NAME=e1-feedback ARGS="--verbose" # the whole folder
make pt ARGS="review list --run e1-feedback"       # receipts parked for a person
make pt ARGS="review show 3 --run e1-feedback"
make pt ARGS="review resolve 3 --accept --run e1-feedback"
make graph                                         # the agents' graph as Mermaid
```

What each step of the graph does is in [Processing a receipt](how-it-works.md#processing-a-receipt).

- `RUN_NAME=<name>` gives the run its own database and traces under `/data/runs/<name>/`
  (created and migrated on first use), so experiment arms never mix. Without it the main
  database is used (run `make migrate` first) and traces go to `/data/runs/main/`. Images are
  stored once, by content, in `/data/images` for every run; the same photo twice in one run is
  reported as a duplicate.
- `ARGS` takes the flags of `paper-trail process`: `--max-attempts N` (1 or 2),
  `--no-feedback` (retry with a generic line instead of the failures),
  `--verbose` (print the feedback and Claude's answers), `--no-color`.
- Exit codes: 0 every receipt completed, was parked for review or paused, 1 `--resume` has
  nothing to resume (unknown receipt, finished run, or a review waiting: use `review resolve`),
  or `--resume`, `--reprocess` or `review resolve` met a receipt the worker (or another command)
  is processing: the message names its job; try again once it has finished. 2 a usage error, 3 a receipt failed, 4 a Claude read stopped the batch (the API failed or
  refused, the answer was cut off, there is no key, or the monthly budget is reached): the
  receipt is `failed` and keeps its checkpoint, and the output prints the
  `paper-trail process --resume <id>` command that continues it.
  A photo that Claude refuses every time, or whose answer is always cut off, has no automatic
  way out yet: `--resume` sends it again and gets the same answer. Such a receipt stays
  `failed`; entering it by hand (`receipts import`) is the way out for now.
- `review resolve ID` takes `--accept` (keep the extraction the review holds) or `--file F` (a
  corrected receipt in the [import format](#importing-receipts)); a receipt that does not add up needs
  `--force --reason TEXT`. It resumes the receipt's graph from its checkpoint and completes it.
- Validation feedback is untrusted (it quotes Claude's reading of the receipt): before it goes
  into the prompt's `<validation_feedback>` block, any such tag inside it is escaped, so text
  printed on a receipt cannot close the block early.
- `make smoke-claude` (opt-in, never part of `make check` or CI) reads the receipts folder with
  the real API into a new run `smoke-claude-<UTC time>` each time, and ends with a table per
  receipt (`process --table`): store, items, the lines' sum against the subtotal, total, whether
  it adds up, the models, attempts and cost. It needs `ANTHROPIC_API_KEY` and costs money.

## Corrections and resuming

Matching never asks a model ([how lines are matched](how-it-works.md#matching-products)).
When it picks the wrong product, correct the line and the next receipt learns from it; a
receipt can also be paused after any step and resumed:

```sh
make process RECEIPTS=costco-1.jpg RUN_NAME=e2     # new products
make process RECEIPTS=costco-2.jpg RUN_NAME=e2     # the same items: linked by their codes, no model call
docker compose run --rm -e PAPER_TRAIL_DATABASE_PATH=/data/runs/e2/paper_trail.db \
  cli receipts show 2                               # the lines and their positions
make pt ARGS="items correct 2 0 --run e2 --new-product 'Kirkland Organic Eggs 24'"
make process RECEIPTS=costco-2.jpg RUN_NAME=e2 ARGS="--reprocess"   # line 0 now by its code
make process RECEIPTS=costco-3.jpg RUN_NAME=e2 ARGS="--interrupt-after validate"
make pt ARGS="process --run e2 --resume 3"          # continues after validate
```

- `items correct RECEIPT_ID POSITION (--product-id N | --new-product NAME) [--category C]
  [--run NAME]` relinks one line (`POSITION` is the line's 0-based `position`, as `receipts
  show` prints it), moves its price observation and remembers the correction, so the next line
  with that text at that store links to the product without a search. When the line starts with
  a product code, the code now names that product (`catalog.learned`), replacing a wrong
  mapping. `--new-product` and `--category` are a person's values (source `person`). It calls
  no model. Exit 1 for an unknown receipt, line or product, 2 for a usage error.
- `--interrupt-after NODE` pauses each receipt after a node of the graph (`extract`,
  `validate`, `supervisor`, `review`, `persist`, `match`, `commit_matches`, `enrich`,
  `commit_enrichment`) and prints the resume command; the checkpoint
  is in the run's database. A saved receipt stays `processing` (stage `matching`) until
  `commit_matches` has linked its lines.
- `--resume ID` continues a paused receipt, or one stopped by a Claude read or by a crash, from its last completed node: nothing before it runs again, and the trace records
  `resumed_from`. It takes no PATH (so it goes through `make pt`, not `make process`), and the
  `--run` the receipt was processed in, but none of the flags that would change the run's
  configuration (`--max-attempts`, `--no-feedback`, `--no-catalog`: exit
  2). A run saved by an older version (OCR and local models) cannot be resumed (exit 2): answer
  its review with a corrected file (`review resolve ID --file F`) or read the photo again with
  `--reprocess`. A receipt
  waiting for a person is answered with `review resolve`, not resumed. A run that fails while
  products are being described leaves the receipt `failed` with its lines, codes and prices
  saved; `--resume` describes the rest and completes it.
- `--reprocess` has Claude read an already stored photo again, on a new thread (a pending
  review is dismissed). Without it the same photo is reported as a duplicate.

## Naming products by their codes

Nothing names a Costco item number for you: the databases do not know store item numbers, so a
new coded product keeps its printed text or Claude's guess until a person names it. The
`catalog` commands do that; only `catalog enrich --apply` calls Claude or the network. Every
command except `plu-import` takes `--run NAME` (that run's database; without it the main one,
which must be migrated).

The Costco workflow:

1. `catalog todo --store costco` lists the products nobody has confirmed yet, most bought first:
   product | name | name source | code | times bought | last paid | last date | a costco.ca
   search link.
2. Open the link in your own browser. The app only prints it; it never requests the link.
3. `catalog add costco 1362394 "Choc Pistachio Bars" --brand Kirkland --size "1.36 kg"
   --category snacks_candy` names the product (shown before and after, then `y` to save;
   `--yes` skips the question). The product the code already names is filled in place, so
   every past line keeps pointing at it; the next receipt links to it by its code.

Its limits: (a) a warehouse item number often matches the costco.ca item number, but not always:
a search may find nothing or a different pack size, so check the page against the printed text;
(b) warehouse-only items (produce, bakery, many fresh items) are usually not online: name them
from the shelf tag, the receipt or a fan site, by hand; (c) the link is a convenience only:
the app copies nothing from the page.

- `catalog todo [--store S] [--limit N] [--json] [--run NAME]`: the list above, coded products
  and uncoded ones (`-` for no code), a guessed name shown as `suggested`.
- `catalog confirm PRODUCT_ID [--run NAME]`: accepts the product's current name, brand, size,
  category and tags as yours (stamped `person`; an empty field stays empty, so a later lookup
  may still fill it). Exit 1 for an unknown product.
- `catalog enrich [--apply] [--run NAME]`: looks up or guesses the stored products (see
  [Product lookups and guesses](how-it-works.md#product-lookups-and-guesses)). Exit
  1 when a product failed (the others are written); when the monthly Claude budget is reached
  the rest are reported as skipped.
- `catalog add STORE CODE ("NAME" | --product ID) [--brand B] [--size "1.36 kg"] [--category C]
  [--replace] [--yes] [--run NAME]`: the code is read with the store's profile (a barcode or a
  PLU names the product at every store, a store's own code only at that store); what you give
  is stamped `person`. Renaming a product a person already named needs `--replace`.
  `--product ID` makes the code name an existing product instead (its old product and lines
  are left as they are, and reported). Exit 2 for an unreadable code or size, 1 for `n`.
- `catalog list [--store S] [--scheme gtin|plu|store] [--json] [--run NAME]`: the stored codes,
  their products and who attached them.
- `catalog remove --store S --code C [--run NAME]`: forgets what a code names; the product and
  its lines stay.
- `catalog lookup "<line or code>" --store S [--run NAME]`: how a line's code is read and which
  source knows it (our product, the PLU table, or none). Writes nothing.
- `catalog plu-import FILE`: writes the PLU table, `<data_dir>/catalog/plu.csv`
  (`PAPER_TRAIL_CATALOG__PLU_PATH`), from the IFPS PLU database export (ifpsglobal.com, "Export all"
  -> .csv). Keep the export under the git-ignored `data/` of the checkout or outside it; it is never
  committed. The `cli` container does not see the checkout, so mount the file for the one run:
  `docker compose run --rm -v "$PWD/data/ifps-plu.csv:/import/ifps-plu.csv:ro" cli catalog
  plu-import /import/ifps-plu.csv`. Each code is named `<Commodity>, <Variety>, <size>` (`4046` ->
  `Avocados, Hass, small`; an empty or `Retailer Assigned` variety and an empty or `All Sizes` size
  are left out), and an organic `9xxxx` code reads as `Organic ` + that name. It prints e.g. `1435
  imported, 113 skipped (111 retailer assigned, 2 duplicate)`: retailer-assigned blocks name no
  product, a repeated code keeps its first row, and rows with an invalid PLU or no commodity are
  skipped too. The file is replaced in one step (a failed import leaves the old one); exit 1 when
  nothing is importable. A running MCP server (`make mcp-up`) reads `plu.csv` once, so restart it
  after an import (`make mcp-down mcp-up`); `process` and the other commands read it fresh each run.
  The worker also reads it once, so restart it too (`make restart`); `web` does not use it.

The agents' tools can be listed and called from the command line too: see
[Tools, allowlists and MCP](how-it-works.md#tools-allowlists-and-mcp).
