# Product codes (barcode lookup PR 1a): requirements to tests

Written from `docs/plans/barcode-lookup.md` (sections 2-4) and the PR 1a slice of the implementation plan (requirements R1-R14, amendments A1-A11, owner answers and rulings I1-I5). The case ids (PC-TC-NN) are the test author's. Every behaviour has one owning test at the layer that enforces it; rows marked (wiring) only check that layers are connected.

Pinned: a GTIN or PLU names one product at every store (`store_key = ''`), a store's own code only at its store; an organic PLU (`93101`) is its own identity, looked up in `plu.csv` as `3101`. Matching and enrichment make zero model requests. A person's field (`field_sources` = `person`) and a person's code mapping are never replaced by a machine.

## Tests removed or rewritten, and the case that owns the behaviour now

Removed:

| Test | Why | Owner now |
|---|---|---|
| `tests/unit/agents/test_matching_graph.py`: `test_s4_ac_02_close_embeddings_link_two_stores_to_one_product`, `test_s4_ac_03_same_links_after_exactly_one_confirmation_with_the_top_candidates`, `test_s4_ac_03_anything_but_a_confident_same_creates_a_new_product`, `test_a_candidate_within_auto_link_wins_without_asking`, `test_thresholds_come_from_settings`, `test_review_note_3_an_answer_about_another_line_is_not_a_confirmation`, `test_s4_ac_04_a_line_far_from_every_product_is_new` | vector and model matching within a store are gone | PC-TC-58 (never asked), PC-TC-54, 56 |
| `tests/unit/agents/test_enrichment_graph.py`: `test_the_enrichment_requests_name_each_new_products_line`, `test_without_examples_the_tag_says_so`, `test_the_enrichment_llm_events_belong_to_the_enrichment_agent`, `test_s4_ac_05_an_invented_category_leaves_the_product_unenriched`, `test_a_low_confidence_answer_leaves_the_product_unenriched`, `test_a_product_found_by_the_vector_index_is_not_enriched` | no model in enrichment | PC-TC-69, 68 |
| `tests/unit/agents/test_stretch_s2_item_code.py::test_ea_ac_13_the_enrichment_request_names_the_line_without_its_code` | no enrichment request | PC-TC-08 (`printed_name`) |
| `tests/contract/test_enrichment_tools.py`: `test_save_enrichment_replaces_the_vector_with_the_enriched_text`, `test_a_failed_embedding_writes_nothing_and_keeps_its_class` | `save_enrichment` removed; describing never embeds | PC-TC-93, 52 |
| `tests/integration/test_vision_enrichment_sqlite.py::test_s4_ac_04_enriched_products_are_searchable_and_their_vectors_replaced` | as above | PC-TC-66, 52; FTS over tags: `tests/integration/test_fts.py` |
| `tests/unit/agents/test_receipt_graph_tools.py`: the `product_similarity_search` out-of-role row | the tool is never called | PC-TC-63, registry allowlist PC-TC-43 |

Rewritten:

| Test | Now asserts | Case |
|---|---|---|
| `tests/unit/agents/test_matching_graph.py` (the rest) | printed-text names, no similarity step, read tools ⊆ {`product_lookup`, `catalog_lookup`}, coded variant of new -> alias -> memory | PC-TC-54, 56, 57, 58, 65 |
| `tests/unit/agents/test_enrichment_graph.py` (the rest) | `describe_from_catalog` payloads, category corrections, crash during enrichment | PC-TC-62, 66, 68, 70, 71, 63 |
| `tests/contract/test_enrichment_tools.py` -> `test_catalog_tools.py` | `describe_from_catalog` fields, sources, person fields, corrections, masking, idempotence | PC-TC-38-42, 92, 93 |
| `tests/contract/test_matching_tools.py` | only confirmed new products embed; global aliases not used; refusal and idempotence tables with codes | PC-TC-46-53, 56 |
| `tests/unit/tools/test_registry.py`, `tests/e2e/test_mcp_server.py`, `tests/e2e/test_cli_tools.py` | allowlists and role listings with `catalog_lookup` and `describe_from_catalog`, without `save_enrichment` | PC-TC-43-45 |
| `tests/unit/agents/test_receipt_graph.py` `MATCHING_STEPS`, `tests/e2e/test_cli_process_mcp.py`, `tests/unit/agents/test_transport_equivalence.py` | the run's tool sequence | PC-TC-62, 89 |
| `tests/unit/agents/test_tracing.py` | tool -> role map, the matching NOTE | PC-TC-61 |
| `tests/unit/agents/test_resume.py` | `state_version`, refused old checkpoints | PC-TC-72 |
| `tests/integration/test_matching_sqlite.py::test_ac_c_03_and_s4_ac_02_memory_on_sqlite` | codes persist across processes | PC-TC-65 |
| `tests/e2e/test_cli_vision_enrichment.py`, `tests/e2e/test_cli_processes.py`, `tests/e2e/test_cli_items.py` | one model call per receipt, no embed requests for printed-text products | PC-TC-89, 75 |
| `tests/unit/application/test_correct_line_item.py` | `person` stamps; a category on an existing product is written | PC-TC-75 |
| `tests/unit/domain/test_agent_core_domain.py` | `MatchSource` values with `catalog` | PC-TC-11 |
| `tests/unit/test_cli_agent_core.py` | `--no-catalog` in the flags and the resume refusal | PC-TC-78 |
| `tests/unit/test_log_privacy.py`, `tests/unit/test_run_config.py`, `tests/unit/adapters/test_trace_document.py` | catalog key in the run config; no content in `catalog.*` logs | PC-TC-84 |
| `tests/support.py` `SCHEMA_HEAD`, `tests/integration/test_migration_0002.py` ... `0005.py`, `test_schema_matches_metadata.py`, `test_cascades.py`, `tests/repo/test_migrations_frozen.py` | head is 0006; older suites target their own revision | PC-TC-18, 22, 23 |

Kept unedited: `test_matching_prompt.py`, `test_enrichment_prompt.py`, `test_graph_shape.py`, `tests/e2e/test_cli_graph.py`, `tests/contract/test_vector_index.py`, the `product_similarity_search` tool tests, `test_config_agent_core_c.py`.

The case-by-requirement index is in the implementation plan's test cases (`PC-TC-01`...`93`, "Requirement -> cases").

## PR 1b: the `catalog` commands and the backfill

Cases PC-TC-101...171 (revision 3), from requirements R15-R19, amendments A3, A4, A7, A8, A11-A16 and the defaults taken for TQ33-TQ41. Synthetic codes and PLU rows only; the owner's IFPS export is never a fixture.

| Suite | Cases |
|---|---|
| `tests/integration/test_migration_0006.py` | 101, 102 (populated upgrade/downgrade/upgrade; a failure at each step of the swap) |
| `tests/unit/domain/rules/test_item_codes.py` | TQ37 (a bare code reads as `<code> X`) |
| `tests/unit/application/test_catalog.py` | 103, 104, 109-115, 120, 151, 152, 154-156 (todo, add, remove on the fakes; `product_code_at`, `parse_size`) |
| `tests/integration/test_catalog_sqlite.py` | 106, 117 (150, the A1 re-read, is structural: `add` takes the request, not its preview; owned by `test_pc_tc_113_a_persons_name_needs_replace`) |
| `tests/unit/application/test_plu_import.py` | 122-124, 164-167 |
| `tests/unit/application/test_catalog_backfill.py` | 125-137, 139 (`link_items` not called), 142, 158, 171 |
| `tests/integration/test_catalog_backfill_sqlite.py` | 139-141, 143, 159, 160, 163 |
| `tests/e2e/test_cli_catalog.py` | 104, 105, 107, 110, 113, 114, 116, 118-121, 124, 138, 144, 145, 153, 168, 169 |
| `tests/unit/test_catalog_offline.py`, `tests/repo/test_catalog_no_network.py` | 108 (the CLI module is now a mandatory part of the scan) |
| `tests/contract/test_receipt_repository.py`, `test_product_repository.py`, `test_correction_repository.py`, `test_vector_index.py` | the repository methods the backfill and `todo` add (`item_lines`, `ids`, `all_aliases`, `update_alias`, `remove_alias`, corrections `list`/`update`, `vectors.has`), on the fake and SQLite |
| `tests/unit/test_cli_agent_core.py` | the `catalog` help listing |

No 1a test is removed or weakened by 1b; the edits above only add tests.

### Manual checks (not automated)

**PC-TC-146 README (docs review).** The README has: `catalog todo` -> open the link in your own browser -> `catalog add`; the limits of design section 1.6 (a-c); one line per `catalog` command with `--run`; "the app never requests the link"; the deploy rule (migrate the live DB only after 1b, run `catalog backfill --apply` before restarting the services); for `plu-import`: where the file comes from (IFPS "Export all -> .csv"), that it is never committed (`/data/` is git-ignored), the name format with one example, the summary line with its reasons, and that running services read `plu.csv` once, so they are restarted after an import. The 1a sentence "`catalog backfill`, still to come, attaches the codes" is updated.

`catalog backfill` was removed once it had run on the live database (chore/delete-catalog-backfill); PC-TC-147 and PC-TC-149 are history.

**PC-TC-147 Backfill on a copy of the live DB.**
1. Stop the services. Copy the DB (with `-wal`/`-shm`) to a dated backup, and work on a second copy through `--run`.
2. `db upgrade`. Record the counts: 48 products, 53 aliases, 0 corrections, lines by `match_source`.
3. `catalog backfill` (dry run): read the report; note the splits (the 4 `llm` and 1 `vector` links are the likely ones) and the merge candidates.
4. `catalog backfill --apply`: its report equals the dry run's. `--apply` again -> "0 changes".
5. `PRAGMA integrity_check` = `ok`; `PRAGMA foreign_key_check` empty; line and observation counts equal the recorded ones; no product that has an ITEM line has `legacy_model` left on a field (a product with no line keeps it, by design): `SELECT id FROM products WHERE field_sources LIKE '%legacy_model%' AND id IN (SELECT product_id FROM line_items WHERE kind = 'item')` is empty.
6. Interruption: on another copy, kill the process mid-apply (Ctrl-C), rerun -> it completes; the next run reports 0 changes; the final counts equal the uninterrupted copy's.

**PC-TC-148 Costco codes in `catalog todo`.** On the backfilled copy: `catalog todo --store costco` lists the Costco items most-bought first. Open three of the printed links in a browser by hand: each search shows the product bought (or nothing: limits a-c). `catalog add costco <code> "<name>"` for one, answer `y`; `todo` no longer lists it; `catalog list --store costco` shows it with source `manual`. Network check: no request to costco.ca from the container (for example, run it with the network disabled).

**PC-TC-149 Live deploy.** After 1b merges: backup, `db upgrade`, `catalog backfill` (dry), `--apply`, `--apply` again = 0 changes, start the services, reprocess one receipt (PC-TC-90). Keep the backup until the next successful run.

**PC-TC-170 The owner's IFPS export.** With the real export (kept outside the checkout or under the git-ignored `/data/`; never committed, never a fixture):
1. `catalog plu-import <file>` prints exactly `1435 imported, 113 skipped (111 retailer assigned, 2 duplicate)`; `plu.csv` has 1,435 data rows.
2. With `--run scratch`, `catalog lookup` of `4046` -> `Avocados, Hass, small`; `94046` -> `Organic Avocados, Hass, small`; `4011` -> `Bananas, Yellow (includes Cavendish)`; `3093` -> `Grapes`; `3170` -> a PLU with no source.
3. `grep -ci ', all sizes' plu.csv` -> 0, and no name ends in `, Retailer Assigned`.
4. Import again -> the same summary and a byte-identical `plu.csv`.
5. `git status` in the checkout shows no new file. Restart the services afterwards (they read the table once).
