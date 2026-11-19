import type {
  ReviewDraftBody,
  ReviewIssueOut,
  ReviewLineBody,
  ReviewOut,
} from "@/features/reviews/api";

/** One bought item as a person edits it: what was paid and the discount taken off. */
export interface ItemRow {
  key: number;
  description: string;
  quantity: string;
  paid: string;
  discount: string;
  /** The printed line, kept as read; a new row's description stands in for it. */
  rawText: string;
  category: ReviewLineBody["category"];
  /** The read unit price, kept while the quantity and regular price are unchanged. */
  unitPriceCents: number | null;
  readQuantity: string;
  readRegularCents: number | null;
  /** The printed text of the item's discount line as read; null for the item's own. */
  discountRawText: string | null;
  /** What the item is sold by, kept as read; null for a new row (unknown). */
  unit: Exclude<ReviewLineBody["unit"], undefined>;
}

/** A deposit, fee or order discount: a signed amount on its own line. */
export interface AdjustmentRow {
  key: number;
  kind: ReviewLineBody["kind"];
  description: string;
  amount: string;
  rawText: string;
}

export interface DraftFields {
  storeName: string;
  purchasedOn: string;
  /** Three letters; every amount of the receipt is in it. */
  currency: string;
  subtotal: string;
  tax: string;
  total: string;
  items: ItemRow[];
  adjustments: AdjustmentRow[];
  /** Kept as read: the form does not show it. */
  paymentLast4: string | null;
}

/** The currency of a receipt typed in from nothing. */
export const DEFAULT_CURRENCY = "CAD";

const CENTS_PER_UNIT = 100;
const CURRENCY = /^[A-Z]{3}$/;
const MONEY = /^-?\d+(\.\d{1,2})?$/;
const QUANTITY = /^\d+(\.\d+)?$/;

/** Cents as a person types an amount, e.g. `8.99` or `-2.50`; empty for none. */
export function centsText(cents: number | null): string {
  if (cents === null) return "";
  const sign = cents < 0 ? "-" : "";
  const magnitude = Math.abs(cents);
  const units = Math.trunc(magnitude / CENTS_PER_UNIT);
  const fraction = String(magnitude % CENTS_PER_UNIT).padStart(2, "0");
  return `${sign}${units}.${fraction}`;
}

/**
 * The cents of a typed amount (`8.99`, `$1,234.5`, `-2`); null when blank, undefined when it
 * is not an amount. Parsed from the digits, never through a float.
 */
export function parseCents(text: string): number | null | undefined {
  const value = text.trim().replace(/[$,\s]/g, "");
  if (value === "") return null;
  if (!MONEY.test(value)) return undefined;
  const negative = value.startsWith("-");
  const [units = "0", fraction = ""] = value.replace("-", "").split(".");
  const cents =
    Number(units) * CENTS_PER_UNIT + Number(fraction.padEnd(2, "0"));
  return negative ? -cents : cents;
}

export function isQuantity(text: string): boolean {
  return QUANTITY.test(text.trim()) && Number(text) > 0;
}

/** Whether two typed quantities are the same number (`2` and `2.0`), compared digit by digit. */
function sameQuantity(typed: string, read: string): boolean {
  const digits = (text: string) => {
    const [units = "", fraction = ""] = text.trim().split(".");
    return `${units.replace(/^0+(?=\d)/, "")}.${fraction.replace(/0+$/, "")}`;
  };
  return isQuantity(typed) && digits(typed) === digits(read);
}

let nextKey = 0;
function newKey(): number {
  nextKey += 1;
  return nextKey;
}

export function emptyItem(): ItemRow {
  return {
    key: newKey(),
    description: "",
    quantity: "1",
    paid: "",
    discount: "",
    rawText: "",
    category: null,
    unitPriceCents: null,
    readQuantity: "",
    readRegularCents: null,
    discountRawText: null,
    unit: null,
  };
}

/** The form of a review's reading: each item with its linked discounts folded in. */
export function fieldsOf(draft: ReviewDraftBody | null): DraftFields {
  if (draft === null) {
    return {
      storeName: "",
      purchasedOn: "",
      currency: DEFAULT_CURRENCY,
      subtotal: "",
      tax: "",
      total: "",
      items: [emptyItem()],
      adjustments: [],
      paymentLast4: null,
    };
  }
  const discounts = new Map<number, number>();
  const discountTexts = new Map<number, string>();
  draft.lines.forEach((line) => {
    if (isItemDiscount(draft.lines, line)) {
      const index = line.applies_to ?? -1;
      discounts.set(index, (discounts.get(index) ?? 0) - line.line_total_cents);
      if (!discountTexts.has(index)) discountTexts.set(index, line.raw_text);
    }
  });
  const items: ItemRow[] = [];
  const adjustments: AdjustmentRow[] = [];
  draft.lines.forEach((line, index) => {
    if (line.kind === "item") {
      const discount = discounts.get(index) ?? 0;
      items.push({
        key: newKey(),
        description: line.description,
        quantity: line.quantity,
        paid: centsText(line.line_total_cents - discount),
        discount: discount === 0 ? "" : centsText(discount),
        rawText: line.raw_text,
        category: line.category,
        unitPriceCents: line.unit_price_cents,
        readQuantity: line.quantity,
        readRegularCents: line.line_total_cents,
        discountRawText: discountTexts.get(index) ?? null,
        unit: line.unit ?? null,
      });
    } else if (!isItemDiscount(draft.lines, line)) {
      adjustments.push({
        key: newKey(),
        kind: line.kind,
        description: line.description,
        amount: centsText(line.line_total_cents),
        rawText: line.raw_text,
      });
    }
  });
  return {
    storeName: draft.store_name,
    purchasedOn: draft.purchased_on ?? "",
    currency: draft.currency,
    subtotal: centsText(draft.subtotal_cents),
    tax: centsText(draft.tax_cents),
    total: centsText(draft.total_cents),
    items,
    adjustments,
    paymentLast4: draft.payment_last4,
  };
}

function isItemDiscount(lines: ReviewLineBody[], line: ReviewLineBody) {
  return (
    line.kind === "discount" &&
    line.applies_to !== null &&
    lines[line.applies_to]?.kind === "item"
  );
}

/** What is wrong with the form, field by field (keys as in `fieldErrors`); empty when none. */
export type FieldErrors = Record<string, string>;

const AMOUNT_HINT = "Use an amount like 8.99";

/**
 * The corrected reading the API takes, or the fields that are not valid. Each item is an
 * ITEM line of its regular price (paid + discount), followed by a DISCOUNT line linked to it
 * when it has one; then the adjustments, at receipt level as read (the API adds no links).
 * The currency is trimmed and upper-cased; every amount is sent in it, cents unchanged.
 */
export function draftOf(
  fields: DraftFields,
): { draft: ReviewDraftBody } | { errors: FieldErrors } {
  const errors: FieldErrors = {};
  const currency = fields.currency.trim().toUpperCase();
  if (!CURRENCY.test(currency))
    errors.currency = "Use a 3-letter code like CAD";
  const amount = (key: string, text: string, required = false) => {
    const cents = parseCents(text);
    if (cents === undefined) errors[key] = AMOUNT_HINT;
    else if (cents === null && required) errors[key] = "Required";
    return cents ?? null;
  };
  const total = amount("total", fields.total, true);
  const subtotal = amount("subtotal", fields.subtotal);
  const tax = amount("tax", fields.tax);
  const lines: ReviewLineBody[] = [];
  fields.items.forEach((row) => {
    const paid = amount(`items.${row.key}.paid`, row.paid, true) ?? 0;
    const discount = amount(`items.${row.key}.discount`, row.discount) ?? 0;
    if (!isQuantity(row.quantity))
      errors[`items.${row.key}.quantity`] = "Use a number like 1 or 0.452";
    if (row.description.trim() === "")
      errors[`items.${row.key}.description`] = "Required";
    const regular = paid + discount;
    const unchanged =
      sameQuantity(row.quantity, row.readQuantity) &&
      regular === row.readRegularCents;
    const description = row.description.trim();
    const rawText = row.rawText === "" ? description : row.rawText;
    const position = lines.length;
    lines.push({
      kind: "item",
      raw_text: rawText,
      description,
      quantity: row.quantity.trim(),
      unit_price_cents: unchanged ? row.unitPriceCents : null,
      line_total_cents: regular,
      category: row.category,
      applies_to: null,
      unit: row.unit,
    });
    if (discount !== 0) {
      lines.push({
        kind: "discount",
        raw_text: row.discountRawText ?? rawText,
        description,
        quantity: "1",
        unit_price_cents: null,
        line_total_cents: -discount,
        category: null,
        applies_to: position,
        unit: null,
      });
    }
  });
  fields.adjustments.forEach((row) => {
    const cents = amount(`adjustments.${row.key}.amount`, row.amount, true);
    lines.push({
      kind: row.kind,
      raw_text: row.rawText,
      description: row.description,
      quantity: "1",
      unit_price_cents: null,
      line_total_cents: cents ?? 0,
      category: null,
      applies_to: null,
      unit: null,
    });
  });
  if (Object.keys(errors).length > 0) return { errors };
  return {
    draft: {
      store_name: fields.storeName.trim(),
      purchased_on: fields.purchasedOn === "" ? null : fields.purchasedOn,
      currency,
      subtotal_cents: subtotal,
      tax_cents: tax,
      total_cents: total ?? 0,
      payment_last4: fields.paymentLast4,
      lines,
    },
  };
}

/**
 * The latest purchase date the editor offers (YYYY-MM-DD): tomorrow in the browser's time
 * zone. The server refuses a date after its own today; a day of slack keeps a phone whose
 * clock or zone runs ahead of the app's from being blocked near midnight.
 */
export function latestPurchaseDate(now: Date): string {
  const tomorrow = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() + 1,
  );
  const month = String(tomorrow.getMonth() + 1).padStart(2, "0");
  const day = String(tomorrow.getDate()).padStart(2, "0");
  return `${String(tomorrow.getFullYear())}-${month}-${day}`;
}

/** Whether the review is about the purchase date (missing, unreadable or implausible). */
export function hasDateIssue(issues: ReviewIssueOut[]): boolean {
  return issues.some((issue) => issue.code === "missing_date");
}

/** What issues of these codes come down to when they are all about one field; else null. */
export function issuesText(codes: readonly string[]): string | null {
  if (codes.length > 0 && codes.every((code) => code === "missing_date"))
    return "Check the date";
  if (codes.length > 0 && codes.every((code) => code === "missing_store"))
    return "Store name missing";
  return null;
}

/** Why a review waits, in a few plain words. */
export function reasonText(
  review: Pick<ReviewOut, "reason" | "issues">,
): string {
  const text = issuesText(review.issues.map((issue) => issue.code));
  if (text !== null) return text;
  switch (review.reason) {
    case "extraction_failed":
      return "Couldn't read the receipt";
    case "low_confidence":
      return "Unsure reading";
    case "unmatched_product":
      return "Unknown product";
    case "validation_failed":
      return "Totals don't add up";
  }
}

const AGE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
];

/** How long ago an instant was, e.g. `3 hours ago`, `yesterday`; `just now` under a minute. */
export function formatAge(
  isoInstant: string,
  now: number = Date.now(),
  locale?: string,
): string {
  const elapsed = now - new Date(isoInstant).getTime();
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size] of AGE_UNITS) {
    if (elapsed >= size)
      return format.format(-Math.floor(elapsed / size), unit);
  }
  return "just now";
}
