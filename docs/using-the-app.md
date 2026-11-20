# Using the app

The phone web app is the main way in. How to reach it from your phone is in the
[README](../README.md#open-it-on-your-phone); the screens are below, then the JSON API
behind them.

## The web app

Open the app in your phone's browser; you can add it to the home screen as a shortcut. (Installing
it as a full-screen app needs HTTPS, which this LAN setup doesn't provide.)
The tab bar is Home · Receipts · **Scan** (the centre button) · Products · Review, with a search
box in the header for products and receipts.

1. **Scan.** Take a photo of the receipt, or pick one (JPEG, PNG, WebP or HEIC), and send it. The
   same photo twice opens the receipt it already made.
2. **Reading.** Claude reads it in the background, usually in about 20 seconds; you can keep
   using the app. Home shows a "Reading your receipt…" card until it is done.
3. **Review.** A receipt whose numbers do not add up, or whose date is missing or implausible,
   waits in the Review tab (the badge counts them): the photo beside the reading, with the
   problems listed. Fix the fields and save, accept the reading as it is, or keep a reading that
   does not add up by giving a reason. A receipt that adds up skips this step.
4. **Receipts.** Listed by purchase date in month groups, with each month's total and number of
   trips. A receipt shows each item once, with its discount folded in ("saved $X") and repeated
   items as ×N; tap an item to open its product.
5. **Products and prices.** Each product appears once, with a sparkline of its price per unit,
   how the newest price compares with the average of earlier purchases (↓8%; within 2% is
   "steady"; "first time" when there is nothing to compare) and the price per L, kg or 100 g when its
   size is known. Chips filter by category, store or "To check". A product's page has a price
   chart with one line per store (1M, 6M, All), the lowest, average and last price per store,
   the cheapest store, and every purchase underneath, each linking to its receipt.
6. **Products to check.** Details Claude guessed for a new product are marked AI until you
   confirm them. Home ("Needs you") and the Review tab link to a queue that shows one product at
   a time: ✓ or ✕ each guessed field, Skip, or "All correct, next". If a field you confirm
   changed while you were looking, nothing is saved and you see the new values (rejecting or
   editing a field saves without that check).
7. **Failed receipts.** A receipt that could not be read says why (the monthly budget is used
   up, Claude was unavailable, no API key, an unreadable photo, or the app stopped while reading
   it) and has a **Try again** button, which continues from where it stopped or reads the photo
   again.

## The web API

The app talks to a JSON API under `/api`; the OpenAPI document is at `/api/openapi.json`, and
the TypeScript client is generated from it (ADR-0007). Writes from a browser must come from the
app's own origin (see [Data and privacy](privacy.md)); curl and scripts send no `Origin`
and are served.

| Endpoint | What it does |
|---|---|
| `POST /api/receipts` | Upload a photo (multipart field `file`): `202` with the queued receipt, `200` with the existing one for a photo already sent, `413` above `UPLOAD__MAX_BYTES`, `415` for anything but a photo |
| `GET /api/receipts` | Receipts by purchase date, newest first (`limit`, `cursor`, `status`, `store`) |
| `GET /api/receipts/{id}` · `/image` | A receipt with its lines and an arithmetic report · its stored photo |
| `POST /api/receipts/{id}/retry` | Queue a `failed` receipt again (`409` if it is not failed or already queued) |
| `GET /api/reviews` · `/{id}` · `POST /{id}/resolve` | Open reviews · one review · accept, correct or force (with a reason) an answer |
| `GET /api/products` · `/to-check` · `/{id}` · `/{id}/purchases` | Products with recent prices · products to check · one product · its purchases |
| `PATCH /api/products/{id}` | Edit a product (each field sent is stamped `person`) |
| `POST /api/products/{id}/confirm` | Confirm its values; with `{fields, expected}`, only those fields, and `409` if a stored value differs from what you saw |
| `GET /api/summary?month=YYYY-MM` | The month's spending by week and store, and what is waiting (reviews, products to check, receipts being read) |
| `GET /api/search?q=` | Products and receipts matching every word (SQLite FTS5) |
| `GET /api/categories` · `/api/health` · `/api/health/ready` | Category values · liveness · readiness |

For example `curl -F file=@receipt.jpg http://192.168.1.10:8420/api/receipts`, then poll
`GET /api/receipts/{id}` until `status` is `completed`, `needs_review` or `failed`.
