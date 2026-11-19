import { describe, expect, it } from "vitest";

import {
  centsText,
  draftOf,
  type DraftFields,
  fieldsOf,
  formatAge,
  latestPurchaseDate,
  parseCents,
  reasonText,
} from "@/features/reviews/draft-form";
import {
  dateIssue,
  line,
  reviewDraft,
  subtotalIssue,
} from "@/test/fixtures/reviews";

function draftFrom(fields: DraftFields) {
  const result = draftOf(fields);
  if (!("draft" in result)) throw new Error(JSON.stringify(result.errors));
  return result.draft;
}

describe("parseCents", () => {
  it.each([
    ["8.99", 899],
    ["8.9", 890],
    ["8", 800],
    ["$1,234.50", 123450],
    ["-2.50", -250],
    [" 0.05 ", 5],
    ["", null],
    ["8.999", undefined],
    ["eight", undefined],
    ["1.2.3", undefined],
  ])("%j is %j", (text, cents) => {
    expect(parseCents(text)).toBe(cents);
  });

  it.each([
    [899, "8.99"],
    [-250, "-2.50"],
    [5, "0.05"],
    [null, ""],
  ])("centsText(%j) is %j", (cents, text) => {
    expect(centsText(cents)).toBe(text);
  });
});

describe("the review form", () => {
  it("folds each linked discount into its item and gives back the same lines", () => {
    const draft = reviewDraft({
      lines: [
        ...reviewDraft().lines,
        line({
          kind: "deposit",
          raw_text: "DEPOSIT",
          description: "Deposit",
          unit_price_cents: null,
          line_total_cents: 10,
          category: null,
        }),
      ],
    });

    const fields = fieldsOf(draft);

    expect(
      fields.items.map(({ description, quantity, paid, discount }) => ({
        description,
        quantity,
        paid,
        discount,
      })),
    ).toEqual([
      {
        description: "Kirkland Organic Eggs",
        quantity: "1",
        paid: "8.99",
        discount: "1.00",
      },
      {
        description: "Kirkland 2% Milk 4 L",
        quantity: "2",
        paid: "6.58",
        discount: "",
      },
    ]);
    expect(fields.adjustments.map((row) => row.amount)).toEqual(["0.10"]);
    expect(draftFrom(fields)).toEqual(draft);
  });

  it("keeps every discount where the reading put it when only the date changes", () => {
    const draft = reviewDraft({
      lines: [
        line({
          raw_text: "12345 BREAD",
          description: "Bread",
          unit_price_cents: null,
        }),
        line({
          kind: "discount",
          raw_text: "INSTANT SAVINGS",
          description: "Bread",
          unit_price_cents: null,
          line_total_cents: -100,
          category: null,
          applies_to: 0,
        }),
        line({
          kind: "discount",
          raw_text: "TPD/12345",
          description: "Order discount",
          unit_price_cents: null,
          line_total_cents: -250,
          category: null,
        }),
      ],
    });

    const fields = fieldsOf(draft);
    const saved = draftFrom({ ...fields, purchasedOn: "2026-11-02" });

    expect(saved.lines).toEqual(draft.lines);
    expect(fields.items.map((row) => row.paid)).toEqual(["8.99"]);
  });

  it.each([
    ["2.0", 329],
    [" 02 ", 329],
    ["3", null],
  ])(
    "keeps the read unit price while the quantity is the same number (%j)",
    (quantity, unitPrice) => {
      const fields = fieldsOf(reviewDraft());
      const [eggs, milk] = fields.items;
      if (!eggs || !milk) throw new Error("two items expected");

      const draft = draftFrom({
        ...fields,
        items: [eggs, { ...milk, quantity }],
      });

      expect(draft.lines[2]?.unit_price_cents).toBe(unitPrice);
    },
  );

  it("drops the read unit price of an item whose price changed", () => {
    const fields = fieldsOf(reviewDraft());
    const [eggs, milk] = fields.items;
    if (!eggs || !milk) throw new Error("two items expected");

    const draft = draftFrom({
      ...fields,
      purchasedOn: "",
      items: [
        { ...eggs, discount: "" },
        { ...milk, paid: "6.85" },
      ],
    });

    expect(draft.purchased_on).toBeNull();
    expect(
      draft.lines.map((row) => [
        row.kind,
        row.unit_price_cents,
        row.line_total_cents,
      ]),
    ).toEqual([
      ["item", null, 899],
      ["item", null, 685],
    ]);
  });

  it("names every field that is not valid and sends nothing", () => {
    const fields = fieldsOf(null);
    const [row] = fields.items;
    if (!row) throw new Error("an empty item expected");

    const result = draftOf({
      ...fields,
      tax: "1.2.3",
      items: [{ ...row, quantity: "0" }],
    });

    expect(result).toEqual({
      errors: {
        total: "Required",
        tax: "Use an amount like 8.99",
        [`items.${String(row.key)}.paid`]: "Required",
        [`items.${String(row.key)}.quantity`]: "Use a number like 1 or 0.452",
        [`items.${String(row.key)}.description`]: "Required",
      },
    });
  });

  it("starts empty with one item when there is no reading", () => {
    const fields = fieldsOf(null);

    expect({ ...fields, items: fields.items.length }).toEqual({
      storeName: "",
      purchasedOn: "",
      currency: "CAD",
      subtotal: "",
      tax: "",
      total: "",
      items: 1,
      adjustments: [],
      paymentLast4: null,
    });
  });
});

describe("draftOf currency", () => {
  it.each(["", "CA", "CADX", "C4D", "€"])(
    "%j is a field error and no draft",
    (currency) => {
      expect(draftOf({ ...fieldsOf(reviewDraft()), currency })).toEqual({
        errors: { currency: "Use a 3-letter code like CAD" },
      });
    },
  );

  it.each([
    ["cad", "CAD"],
    [" usd ", "USD"],
  ])("%j is sent as %j with every amount unchanged", (typed, sent) => {
    const draft = draftFrom({ ...fieldsOf(reviewDraft()), currency: typed });

    expect(draft).toEqual({ ...reviewDraft(), currency: sent });
  });

  it("takes the reading's currency, and CAD without a reading", () => {
    expect(fieldsOf(reviewDraft({ currency: "USD" })).currency).toBe("USD");
    expect(fieldsOf(null).currency).toBe("CAD");
  });
});

describe("reasonText", () => {
  it.each([
    ["validation_failed", [subtotalIssue], "Totals don't add up"],
    ["validation_failed", [dateIssue], "Check the date"],
    ["validation_failed", [dateIssue, subtotalIssue], "Totals don't add up"],
    ["extraction_failed", [], "Couldn't read the receipt"],
  ] as const)("%s with %j", (reason, issues, text) => {
    expect(reasonText({ reason, issues: [...issues] })).toBe(text);
  });
});

describe("formatAge", () => {
  const now = Date.parse("2026-11-03T12:00:00Z");

  it.each([
    ["2026-11-03T11:59:30Z", "just now"],
    ["2026-11-03T11:55:00Z", "5 minutes ago"],
    ["2026-11-03T09:00:00Z", "3 hours ago"],
    ["2026-11-02T11:00:00Z", "yesterday"],
    ["2026-10-27T12:00:00Z", "7 days ago"],
  ])("%s is %s", (instant, text) => {
    expect(formatAge(instant, now, "en")).toBe(text);
  });
});

describe("latestPurchaseDate", () => {
  it.each([
    [new Date(2026, 10, 6, 12, 0), "2026-11-07"],
    [new Date(2026, 10, 30, 23, 59), "2026-12-01"],
    [new Date(2026, 11, 31, 0, 1), "2027-01-01"],
  ])("on %s it is %s", (now, latest) => {
    expect(latestPurchaseDate(now)).toBe(latest);
  });
});
