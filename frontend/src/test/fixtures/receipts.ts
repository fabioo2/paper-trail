/**
 * Receipt API payloads for tests, typed from the generated OpenAPI schema so a backend change
 * that breaks the frontend breaks these fixtures at compile time. Defaults describe one
 * consistent receipt: eggs 8.99 + bananas 1.36 kg x 1.50 = 2.04, subtotal 11.03, tax 0.52,
 * total 11.55.
 */
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type MoneyOut = Schemas["MoneyOut"];
export type LineItemOut = Schemas["LineItemOut"];
export type ReceiptSummaryOut = Schemas["ReceiptSummaryOut"];
export type ReceiptPageOut = Schemas["ReceiptPageOut"];
export type ReceiptDetailOut = Schemas["ReceiptDetailOut"];

export function cad(amount_cents: number): MoneyOut {
  return { amount_cents, currency: "CAD" };
}

export function receiptSummary(
  overrides: Partial<ReceiptSummaryOut> = {},
): ReceiptSummaryOut {
  return {
    id: 1,
    status: "completed",
    store_name: "COSTCO WHOLESALE #1234",
    purchased_on: "2026-11-01",
    total: cad(1155),
    item_count: 2,
    created_at: "2026-11-01T18:30:00Z",
    ...overrides,
  };
}

export function receiptPage(
  items: ReceiptSummaryOut[],
  next_cursor: string | null = null,
): ReceiptPageOut {
  return { items, next_cursor };
}

export function lineItem(overrides: Partial<LineItemOut> = {}): LineItemOut {
  return {
    id: 10,
    position: 0,
    kind: "item",
    raw_text: "KS ORG EGGS 24CT",
    description: "Kirkland Organic Eggs 24 ct",
    quantity: "1",
    unit_price: cad(899),
    line_total: cad(899),
    product_id: null,
    category: "dairy_eggs",
    ...overrides,
  };
}

export function receiptDetail(
  overrides: Partial<ReceiptDetailOut> = {},
): ReceiptDetailOut {
  return {
    ...receiptSummary({ id: 7 }),
    stage: "done",
    subtotal: cad(1103),
    tax: cad(52),
    payment_last4: "1111",
    has_image: false,
    items: [
      lineItem(),
      lineItem({
        id: 11,
        position: 1,
        raw_text: "BANANAS 1.36KG",
        description: "Bananas",
        quantity: "1.36",
        unit_price: cad(150),
        line_total: cad(204),
        category: "produce",
      }),
    ],
    arithmetic: { ok: true, issues: [] },
    ...overrides,
  };
}
