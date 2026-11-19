import { describe, expect, it } from "vitest";

import {
  type ReceiptRow,
  receiptRows,
} from "@/features/receipts/receipt-lines";
import { cad, lineItem, type LineItemOut } from "@/test/fixtures/receipts";

/** Line `position` (id 100 + position): an unmatched 1 x `cents` item unless overridden. */
function line(
  position: number,
  cents: number,
  overrides: Partial<LineItemOut> = {},
): LineItemOut {
  return lineItem({
    id: 100 + position,
    position,
    raw_text: `ITEM ${String(position)}`,
    description: `Item ${String(position)}`,
    unit_price: cad(cents),
    line_total: cad(cents),
    category: null,
    ...overrides,
  });
}

function discount(
  position: number,
  cents: number,
  appliesTo: number | null,
): LineItemOut {
  return line(position, cents, {
    kind: "discount",
    raw_text: "TPD/1362394",
    description: "Instant savings",
    unit_price: null,
    applies_to_position: appliesTo,
  });
}

const CHOC = {
  raw_text: "1362394 CHOC PISTACH",
  description: "Choc Pistach",
  product_id: 7,
  product_name: "Chocolate Pistachio",
  product_category: "snacks_candy",
} as const;

const SAME = { raw_text: "ITEM 0", description: "Item 0" } as const;
const BANANAS = {
  raw_text: "BANANAS",
  description: "Bananas",
  product_id: 3,
  product_name: "Bananas",
} as const;
const BANANAS_ROW = { name: "Bananas", productId: 3 } as const;

function row(position: number, overrides: Partial<ReceiptRow>): ReceiptRow {
  return {
    key: 100 + position,
    kind: "item",
    name: `Item ${String(position)}`,
    printed: null,
    category: null,
    productId: null,
    quantity: "1",
    weighed: false,
    pricePerUnit: null,
    unit: "each",
    paid: cad(0),
    saved: null,
    ...overrides,
  };
}

describe("receiptRows", () => {
  it.each<[string, LineItemOut[], ReceiptRow[]]>([
    [
      "an unmatched line keeps its description; printed text that reads the same is hidden",
      [line(0, 899)],
      [row(0, { paid: cad(899) })],
    ],
    [
      "a matched line takes the product's name and category and shows the printed text",
      [line(0, 1699, CHOC)],
      [
        row(0, {
          name: "Chocolate Pistachio",
          printed: "CHOC PISTACH",
          category: "snacks_candy",
          productId: 7,
          paid: cad(1699),
        }),
      ],
    ],
    [
      "a linked discount folds into its item and is not a row",
      [line(0, 2999), discount(1, -600, 0)],
      [row(0, { paid: cad(2399), saved: cad(600) })],
    ],
    [
      "an unlinked discount, a fee and a deposit stay rows of their own",
      [
        line(0, 899),
        discount(1, -100, null),
        line(2, 25, { kind: "fee", description: "Bag fee" }),
        line(3, 10, { kind: "deposit", description: "Deposit" }),
      ],
      [
        row(0, { paid: cad(899) }),
        row(1, {
          kind: "discount",
          name: "Instant savings",
          printed: "TPD/1362394",
          paid: cad(-100),
        }),
        row(2, {
          kind: "fee",
          name: "Bag fee",
          printed: "ITEM 2",
          paid: cad(25),
        }),
        row(3, {
          kind: "deposit",
          name: "Deposit",
          printed: "ITEM 3",
          paid: cad(10),
        }),
      ],
    ],
    [
      "a saving over several items reads as a multi-buy saving",
      [
        line(0, -600, {
          kind: "discount",
          raw_text: "MULTIPLE",
          description: "Multiple",
        }),
      ],
      [
        row(0, {
          kind: "discount",
          name: "Multi-buy saving",
          printed: "MULTIPLE",
          paid: cad(-600),
        }),
      ],
    ],
    [
      "a discount that names a line that is not an item stays a row",
      [line(0, 25, { kind: "fee", description: "Fee" }), discount(1, -25, 0)],
      [
        row(0, { kind: "fee", name: "Fee", printed: "ITEM 0", paid: cad(25) }),
        row(1, {
          kind: "discount",
          name: "Instant savings",
          printed: "TPD/1362394",
          paid: cad(-25),
        }),
      ],
    ],
    [
      "the same product five times, each with its discount, is one ×5 row with summed amounts",
      [0, 2, 4, 6, 8].flatMap((position) => [
        line(position, 1699, CHOC),
        discount(position + 1, -400, position),
      ]),
      [
        row(0, {
          name: "Chocolate Pistachio",
          printed: "CHOC PISTACH",
          category: "snacks_candy",
          productId: 7,
          quantity: "5",
          paid: cad(6495),
          saved: cad(2000),
        }),
      ],
    ],
    [
      "the same product apart on the receipt still collapses, at its first place",
      [line(0, 1699, CHOC), line(1, 899), line(2, 1699, CHOC)],
      [
        row(0, {
          name: "Chocolate Pistachio",
          printed: "CHOC PISTACH",
          category: "snacks_candy",
          productId: 7,
          quantity: "2",
          paid: cad(3398),
        }),
        row(1, { paid: cad(899) }),
      ],
    ],
    [
      "identical unmatched lines collapse only while one follows the other",
      [line(0, 299), line(1, 299, SAME), line(2, 899), line(3, 299, SAME)],
      [
        row(0, { quantity: "2", paid: cad(598) }),
        row(2, { paid: cad(899) }),
        row(3, { name: "Item 0", paid: cad(299) }),
      ],
    ],
    [
      "whole quantities add up; a kg or lb line, even a whole one, keeps its quantity and never collapses",
      [
        line(0, 658, { quantity: "2" }),
        line(1, 658, { quantity: "2.000", ...SAME }),
        line(2, 204, { quantity: "1.36", unit: "kg", ...BANANAS }),
        line(3, 150, { quantity: "1", ...BANANAS }),
        line(4, 300, { quantity: "2", unit: "kg", ...BANANAS }),
        line(5, 150, { quantity: "1", ...BANANAS }),
        line(6, 300, { quantity: "2", unit: "lb", ...BANANAS }),
      ],
      [
        row(0, { quantity: "4", paid: cad(1316) }),
        row(2, {
          ...BANANAS_ROW,
          quantity: "1.36",
          weighed: true,
          unit: "kg",
          pricePerUnit: cad(204),
          paid: cad(204),
        }),
        row(3, { ...BANANAS_ROW, quantity: "2", paid: cad(300) }),
        row(4, {
          ...BANANAS_ROW,
          quantity: "2",
          weighed: true,
          unit: "kg",
          pricePerUnit: cad(300),
          paid: cad(300),
        }),
        row(6, {
          ...BANANAS_ROW,
          quantity: "2",
          weighed: true,
          unit: "lb",
          pricePerUnit: cad(300),
          paid: cad(300),
        }),
      ],
    ],
    [
      "a line read before units were kept is weighed when its quantity is fractional",
      [
        line(0, 99, { quantity: "0.66", unit: null }),
        line(1, 150, { quantity: "1", unit: null }),
        line(2, 150, { quantity: "1", unit: null, ...SAME }),
      ],
      [
        row(0, {
          quantity: "0.66",
          weighed: true,
          unit: null,
          pricePerUnit: cad(99),
          paid: cad(99),
        }),
        row(1, { quantity: "1", unit: null, paid: cad(150) }),
        row(2, { name: "Item 0", unit: null, paid: cad(150) }),
      ],
    ],
  ])("%s", (_, items, expected) => {
    expect(receiptRows(items)).toEqual(expected);
  });
});
