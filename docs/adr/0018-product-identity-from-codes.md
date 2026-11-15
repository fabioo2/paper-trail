# 18. Products are identified by the code on the receipt; descriptions come only from data

Date: 2026-11-15

## Status

Accepted (the within-store part of `docs/plans/barcode-lookup.md`; comparison groups across stores are planned separately)

## Context

Matching linked receipt lines to products by embedding distance and a model's confirmation, and enrichment asked a model for each new product's name, brand, size and category. On the first real receipts about 40 % of those guesses were wrong (`PC UNS BABYWIPES` became "Pampers", `CM CORN 7IN 6PK` "Chiquita canned corn"): a receipt abbreviation does not say what the product is, and a model fills the gap with a plausible brand. Most lines, though, start with a code that does identify the product: a barcode (GTIN), a produce PLU, or a store's own item number (every Costco line).

## Decision

1. **Identity within a store is the code**, matched exactly. A GTIN or PLU names one product at every store; a store's own item or in-store code names one only at its store (`product_codes`, unique on `(scheme, store_key, code)`). The code is parsed from the line's raw text by a pure rule (`domain/rules/item_codes.py`, GS1 check digits, Costco's "every code is a store item" profile, organic PLUs as their own identity). Matching asks no model: a coded line links to the code's product (`catalog`), else to a person's correction (`memory`, which teaches the code), else becomes a new product named by a data source (the PLU table) or by its printed text. A line without a code links only through a person's correction or an exact alias at the same store; products are never linked across stores by their text.
2. **Every product field carries its source** (`products.field_sources`: `receipt_text`, `person`, `plu`, `off`, `off_llm`, `legacy_model`). No model writes a name, brand, size or category: an unknown product keeps its printed text and an empty category until a person or a data source names it. A person's value is never replaced by a machine's, and a person's code mapping replaces a machine's but not the reverse. Values written by a model before this decision are stamped `legacy_model` by migration 0006 and never count as confirmed.

## Consequences

- `matching/v1` and `enrichment/v1` stay on disk, unused; the matching and enrichment agents make no model requests. Only products named by a person or a data source get an embedding.
- Learning is explicit: `items correct` on a coded line maps its code to the corrected product (`catalog.learned`).
- New tools `catalog_lookup` and `describe_from_catalog` replace `save_enrichment` (ADR-0013 amendment).
- The app never contacts a store's website or any other catalog service; a store search link is printed for a person to open.
- Products bought at different stores without a shared barcode or PLU stay separate until comparison groups are built.
