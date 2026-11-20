# Running and maintaining it

Setting Paper Trail up is in the [README](../README.md#set-up). This page has the rest: the
API key and the monthly budget, every Make target and setting, backups and upgrades.

## The API key

The key lives only in `.env` (git-ignored). Compose hands `.env` to `web`, `worker`, `cli` and
`mcp` through `env_file` (a key exported in your shell is not passed on); `migrate` blanks it.
The dev tooling containers (`make test`, `make shell`) mount the repository at `/app`, and the
app's settings read `/app/.env` there too, so the key is readable inside them; the tests never
call the API (they use fakes). Without a key the app starts and everything that needs no model
works (browsing, imports, the catalog, prices, reviews); a receipt sent to the agents fails with
a message naming `ANTHROPIC_API_KEY`, and can be tried again (Try again on the phone) once the
key is set.

## Cost and the monthly budget

Claude calls cost money: about $0.001 per receipt on the extract model and about $0.04 for a
retry on the retry model. `PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD` (default 5.00) caps the
spend per UTC calendar month: it is checked before each call, and once it is reached receipts
fail, resumable, until the cap is raised or the month ends. Each call reserves its largest
possible cost before it starts, so processes running at once are admitted one at a time and
cannot all slip under the cap. A read is up to three attempts (a rate limit, an overloaded API,
a lost connection or a timeout is retried after a short pause), each reserved and charged on
its own. An attempt that starts just under the cap can overshoot it by what it costs; one that
times out or loses its connection may have been billed without saying for how much, so it is
charged at its largest possible cost: the cap can be overshot by one attempt's worth at most.
Every attempt's tokens and cost are recorded in the main database (`claude_calls`), whatever
`RUN_NAME` the receipt is processed in, so a named run never starts a budget of its own; the
main database must therefore be migrated before any run (`make migrate`; a named run exits 1
otherwise and never migrates it itself).

## Make targets

`make` (or `make help`) lists them. Every target runs in Docker.

| Target | What it does |
|---|---|
| `make build` | Build the runtime image and the dev tooling images |
| `make up` / `make down` | Start / stop the runtime stack (`migrate`, `web`, `worker`); `down` keeps the data volume |
| `make restart` | Restart `web` and `worker` |
| `make logs` / `make ps` | Follow the logs / show the containers and their health |
| `make migrate` | Apply the migrations baked into the built image (`make build migrate` after adding one) |
| `make backup` | Back up the database now (see [Backups and restoring](#backups-and-restoring)) |
| `make revision m="..."` | Create a hand-written migration (`id=0002` sets the revision id) |
| `make shell` / `make fe-shell` | A shell in the Python / frontend dev container |
| `make lock` | Re-resolve Python dependencies into `uv.lock` |
| `make lint` / `make format` | Ruff lint + format check + import-linter / auto-fix and format |
| `make typecheck` | mypy `--strict` |
| `make test` | All Python suites in parallel with branch coverage (fails under 90 %) |
| `make test-fast` | Unit, contract and integration suites in parallel, no coverage: the day-to-day loop |
| `make test-unit` / `make test-integration` | The unit and contract suites / the integration suite |
| `make fe-lint` / `make fe-format` / `make fe-typecheck` / `make fe-test` / `make fe-build` | ESLint + Prettier check / Prettier write / `tsc` / Vitest with coverage / Vite build (each first runs `fe-deps`) |
| `make fe-deps` / `make fe-install` | Reinstall frontend dependencies if `package-lock.json` changed / always (`npm ci`) |
| `make fe-dev` | Vite dev server on `:5173`, proxying `/api` to `web` |
| `make api-client` / `make api-check` | Regenerate `frontend/openapi.json` and the TS schema / fail if they drifted from the backend |
| `make secrets` | Self-test the secret scan, then scan git history and the working tree (gitleaks) |
| `make smoke` | Start the stack with your `.env` and smoke-test it from inside the network (leaves it running) |
| `make container-checks` | Inspect the built image and a throwaway stack (size budget, user, hardening) |
| `make pre-commit` / `make hooks` | Run the pre-commit hooks on every file and scan staged changes / point git at `.githooks` |
| `make check` | Fast gate: lint, types, tests, frontend checks, API contract, secrets |
| `make ci` | Full gate: `build`, `check`, `smoke`, `container-checks`; leaves the stack running |
| `make process` | Process receipts with the agents: `RECEIPTS=<file or folder>`, `RUN_NAME=<run>`, `ARGS="<flags>"` |
| `make smoke-claude` | Live smoke: Claude reads `RECEIPTS` into a new run and prints a table (needs `ANTHROPIC_API_KEY`; costs money; never part of `check` or CI) |
| `make pt ARGS="..."` | Run any `paper-trail` command in the `cli` service, e.g. `ARGS="review list"` |
| `make graph` | Print the agents' graph as Mermaid |
| `make mcp-up` / `make mcp-down` | Start / stop and remove the MCP tool server (`127.0.0.1:8421`); the rest of the stack is untouched |
| `make outdated` | Report newer Python, npm and base-image versions (changes nothing) |
| `make clean` | Remove caches, coverage and build output (never the data volume) |

## Configuration

Every setting is an environment variable in `.env` (git-ignored), documented with its default in
[`.env.example`](../.env.example); all are optional. After changing one, `make up` recreates the containers it affects (`make
restart` does not re-read `.env`).

Deployment (read by compose):

- `PAPER_TRAIL_BIND_ADDR` (default `127.0.0.1`): the host address the web port is published on.
  Set it to the server's LAN address to use the app from your phone. `PAPER_TRAIL_PORT` (default
  `8420`) is the host port.
- `PAPER_TRAIL_DATA_VOLUME` (default the `paper-trail-data` named volume): where the database and
  images live; an absolute host folder works too, owned by UID 1000.
- `PAPER_TRAIL_RECEIPTS_DIR` (default `./receipts`): the folder of photos `make process` reads.

The app:

- `PAPER_TRAIL_WEB__ALLOWED_HOSTS` (default: none): the host names or IPs you open the app by,
  comma-separated, without scheme or port (e.g. `192.168.1.10,my-server`), besides `localhost`,
  `127.0.0.1`, `::1` and `web`. Any other `Host` is refused with 400
  ([ADR-0020](adr/0020-host-and-origin-checks.md)), so add every name you use, the
  WireGuard address included.
- `ANTHROPIC_API_KEY` (no prefix): the Claude API key. Without it a receipt fails with a message
  naming this variable.
- `PAPER_TRAIL_CLAUDE__EXTRACT_MODEL` / `__RETRY_MODEL` (defaults `claude-haiku-5-5` /
  `claude-opus-5-5`; a model without a known price is refused), `__TIMEOUT_S` (default `300`,
  per request, at most `600`: the retry model can take minutes, and a request given up on is
  still billed) and `__MONTHLY_BUDGET_USD` (default `5.00`, per UTC month, checked before each
  call; `0` stops every call).
- `PAPER_TRAIL_GRAPH__MAX_ATTEMPTS` (default `2`: the extract model, then one retry),
  `__FEEDBACK` (default `true`: the retry is told what failed) and `__ENRICHMENT` (default
  `true`: describe new products).
- `PAPER_TRAIL_LOOKUP__OPEN_FACTS` (default `true`), `__UPCITEMDB` (default `false`) and
  `__TIMEOUT_S` (default `10`): the product databases asked for a barcode (see
  [Product lookups and guesses](how-it-works.md#product-lookups-and-guesses)).
- `PAPER_TRAIL_CATALOG__ENABLED` (default `true`: match coded lines by their code) and
  `__PLU_PATH` (default `<data dir>/catalog/plu.csv`).
- `PAPER_TRAIL_UPLOAD__MAX_BYTES` (default `15000000`) and `__MAX_PIXELS` (default `40000000`):
  larger uploads are refused.
- `PAPER_TRAIL_BACKUP__AT` (default `03:30`, local time) and `__KEEP` (default `14`): the
  nightly backup (see [Backups and restoring](#backups-and-restoring)).
- `PAPER_TRAIL_WORKER__SHUTDOWN_GRACE_S` (default `90`): how long a running step may take to
  finish after the worker is asked to stop; keep the worker's `stop_grace_period` in
  `compose.yaml` (100 s) above it. `__POLL_INTERVAL_S` (default `1`) and
  `__HEARTBEAT_INTERVAL_S` (default `10`) rarely need changing.
- `PAPER_TRAIL_TOOLS__TRANSPORT` (default `inprocess`, or `mcp`): how the agents call their
  tools; `PAPER_TRAIL_MCP__URL` is the server `tools --transport mcp` uses, and
  `PAPER_TRAIL_MCP__TIMEOUT_S` (default `330`) must be above `PAPER_TRAIL_CLAUDE__TIMEOUT_S`.
- `PAPER_TRAIL_LOG_LEVEL` (default `INFO`) and `PAPER_TRAIL_LOG_FORMAT` (`json`, or `console`).

## Backups and restoring

Every night at `PAPER_TRAIL_BACKUP__AT` (default `03:30`, local time) the worker copies the
database to `/data/backups/paper_trail-YYYYMMDD.db` on the data volume and keeps the newest
`PAPER_TRAIL_BACKUP__KEEP` (default `14`). A worker that was stopped at that time makes the
day's backup as soon as it runs again. The copy is taken online (the app keeps working), checked
with SQLite's `integrity_check`, and only then given its name, so every file there is a whole,
checked database; the worker logs `backup.written` or `backup.failed`. `make backup` makes
one now (replacing today's). The receipt photos (`/data/images`) are not in it, and nothing is
copied off the server: the backups live on the same volume as the database, so copy
`/data/backups` to another disk yourself if you want them to survive that disk.

To restore one, stop the stack, move the database aside (with its `-wal` and `-shm` files, if
any), copy the backup in its place and start the stack again:

```bash
make down
docker compose run --rm --entrypoint sh cli -c 'cd /data \
  && for f in paper_trail.db paper_trail.db-wal paper_trail.db-shm; do \
       if [ -e "$f" ]; then mv "$f" "before-restore-$f"; fi; done \
  && cp backups/paper_trail-20261118.db paper_trail.db'
make up
```

Receipts recorded after that backup are gone from the database, but their photos are still in
`/data/images`: upload them again.

## Upgrading

Back up the database (`make backup`), then `git pull` and `make build up`: the `migrate`
service applies any new migration before `web` and `worker` start.

### From the local-model version

Earlier versions read receipts on your own hardware, with OCR and local models. To upgrade, in
this order:

1. **Resolve open reviews and failed runs before upgrading**, with the old version still
   running: `review list` must be empty and no receipt may be `processing` or `failed`. A run
   saved by the old version cannot be resumed by the new one (`process --resume` refuses it);
   after the upgrade its only ways out are answering its review with a corrected file
   (`review resolve ID --file F`) or reading the photo again (`process PHOTO --reprocess`).
2. **Back up the database** (`make backup`): migration 0007 drops the product vectors,
   which cannot be recovered, and turns any receipt still `waiting_for_gpu` into `failed`.
3. `make build migrate`, then `make up`.

These variables are no longer read and can be removed from `.env` (they are ignored if left):
`PAPER_TRAIL_OCR__*`, `PAPER_TRAIL_OLLAMA__*`, `PAPER_TRAIL_MATCHING__*`,
`PAPER_TRAIL_GRAPH__MAX_TEXT_ATTEMPTS`, `PAPER_TRAIL_GRAPH__MAX_VISION_ATTEMPTS`,
`PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW`, `PAPER_TRAIL_GRAPH__MIN_CONFIDENCE`,
`PAPER_TRAIL_PROMPTS__VISION_VERSION`, `PAPER_TRAIL_PROMPTS__MATCHING_VERSION`,
`PAPER_TRAIL_PROMPTS__ENRICHMENT_VERSION`,
`PAPER_TRAIL_PROMPTS__EXAMPLES`. Remove `PAPER_TRAIL_PROMPTS__EXTRACTION_VERSION` too if it is
set (for example to `v2`): only `v3`, the default, exists now, and any other value stops
`process` with exit 2. The separate model host and its compose stack are no longer needed.

Migration 0008 (product lookups) adds the `product_lookups` table and turns the old `off_llm`
field source into `llm`: a product named that way is no longer confirmed and shows in
`catalog todo`. Back up first; its downgrade turns `llm` back into `off_llm` and `upc` into
`off`. Then `catalog enrich` (a dry run) shows what lookups and guesses would fill for the
products already stored, and `catalog enrich --apply` does it.
