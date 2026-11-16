/**
 * Product API payloads for tests, typed from the generated OpenAPI schema. The default is one
 * guessed product: Claude named it and chose its category, nobody has confirmed it.
 */
import type { components } from "@/api/schema";
import { cad } from "@/test/fixtures/receipts";

type Schemas = components["schemas"];
export type ProductOut = Schemas["ProductOut"];
export type ProductSummaryOut = Schemas["ProductSummaryOut"];
export type CategoryOut = Schemas["CategoryOut"];

export const categories: CategoryOut[] = [
  { value: "produce", label: "Produce" },
  { value: "dairy_eggs", label: "Dairy & eggs" },
  { value: "pantry", label: "Pantry" },
];

export function product(overrides: Partial<ProductOut> = {}): ProductOut {
  return {
    id: 5,
    canonical_name: "Kirkland Organic Eggs",
    brand: "Kirkland Signature",
    size_value: "24",
    size_unit: "each",
    category: "dairy_eggs",
    tags: ["organic", "free-run"],
    field_sources: { canonical_name: "llm", category: "llm" },
    confirmed: false,
    ...overrides,
  };
}

export function productSummary(
  overrides: Partial<ProductSummaryOut> = {},
): ProductSummaryOut {
  return {
    ...product(),
    times_bought: 2,
    last_purchase: {
      purchased_on: "2026-11-01",
      store_name: "COSTCO WHOLESALE #1234",
      paid: cad(899),
    },
    ...overrides,
  };
}
