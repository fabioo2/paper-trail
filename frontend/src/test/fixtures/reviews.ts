/**
 * Review API payloads for tests, typed from the generated OpenAPI schema. The default review
 * holds a Costco reading whose eggs line carries a 1.00 discount and whose subtotal does not
 * add up: eggs 9.99 - 1.00 + milk 2 x 3.29 = 15.57, read as 15.75; tax 0.79, total 16.54.
 */
import type { components } from "@/api/schema";
import { cad } from "@/test/fixtures/receipts";

type Schemas = components["schemas"];
export type ReviewOut = Schemas["ReviewOut"];
export type ReviewSummaryOut = Schemas["ReviewSummaryOut"];
export type ReviewDraftBody = Schemas["ReviewDraftBody"];
export type ReviewLineBody = Schemas["ReviewLineBody"];
export type ReviewIssueOut = Schemas["ReviewIssueOut"];

export const subtotalIssue: ReviewIssueOut = {
  code: "items_vs_subtotal",
  message: "items add up to 15.57 but the subtotal is 15.75",
  position: null,
};

export const dateIssue: ReviewIssueOut = {
  code: "missing_date",
  message:
    'the purchase date "26/13/01" is not a date up to today: copy the date exactly as printed',
  position: null,
};

export function line(overrides: Partial<ReviewLineBody> = {}): ReviewLineBody {
  return {
    kind: "item",
    raw_text: "KS ORG EGGS 24CT",
    description: "Kirkland Organic Eggs",
    quantity: "1",
    unit_price_cents: 999,
    line_total_cents: 999,
    category: "dairy_eggs",
    applies_to: null,
    unit: null,
    ...overrides,
  };
}

export function reviewDraft(
  overrides: Partial<ReviewDraftBody> = {},
): ReviewDraftBody {
  return {
    store_name: "COSTCO WHOLESALE #1234",
    purchased_on: "2026-11-01",
    currency: "CAD",
    subtotal_cents: 1575,
    tax_cents: 79,
    total_cents: 1654,
    payment_last4: "1111",
    lines: [
      line(),
      line({
        kind: "discount",
        quantity: "1",
        unit_price_cents: null,
        line_total_cents: -100,
        category: null,
        applies_to: 0,
      }),
      line({
        raw_text: "KS 2% MILK 4L",
        description: "Kirkland 2% Milk 4 L",
        quantity: "2",
        unit_price_cents: 329,
        line_total_cents: 658,
        unit: "each",
      }),
    ],
    ...overrides,
  };
}

export function review(overrides: Partial<ReviewOut> = {}): ReviewOut {
  return {
    id: 7,
    receipt_id: 3,
    reason: "validation_failed",
    status: "open",
    receipt_status: "needs_review",
    answer_queued: false,
    issues: [subtotalIssue],
    created_at: "2026-11-01T18:30:00Z",
    image_url: "/api/receipts/3/image",
    draft: reviewDraft(),
    ...overrides,
  };
}

export function reviewSummary(
  overrides: Partial<ReviewSummaryOut> = {},
): ReviewSummaryOut {
  return {
    id: 7,
    receipt_id: 3,
    reason: "validation_failed",
    issues: [subtotalIssue],
    created_at: "2026-11-01T18:30:00Z",
    reading: {
      store_name: "COSTCO WHOLESALE #1234",
      purchased_on: "2026-11-01",
      total: cad(1654),
      item_count: 2,
    },
    ...overrides,
  };
}
