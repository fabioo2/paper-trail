/**
 * Home and search payloads for tests, typed from the generated OpenAPI schema. The default
 * summary is a brand-new install's: nothing bought, nothing waiting.
 */
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type MonthSummaryOut = Schemas["MonthSummaryOut"];
export type SearchOut = Schemas["SearchOut"];

export function monthSummary(
  overrides: Partial<MonthSummaryOut> = {},
): MonthSummaryOut {
  return {
    month: "2026-11",
    spent: [],
    saved: [],
    trips: 0,
    weeks: [
      { start: "2026-11-01", end: "2026-11-07", spent: [] },
      { start: "2026-11-08", end: "2026-11-14", spent: [] },
      { start: "2026-11-15", end: "2026-11-21", spent: [] },
      { start: "2026-11-22", end: "2026-11-30", spent: [] },
    ],
    stores: [],
    open_reviews: 0,
    unconfirmed_products: 0,
    processing: 0,
    recent: [],
    ...overrides,
  };
}

export function searchResults(overrides: Partial<SearchOut> = {}): SearchOut {
  return { products: [], receipts: [], ...overrides };
}
