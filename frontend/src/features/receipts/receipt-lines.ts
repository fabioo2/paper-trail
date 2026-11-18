import type { Category } from "@/features/products/api";
import type { LineItemOut } from "@/features/receipts/api";
import type { MoneyOut } from "@/features/receipts/money";

/** One row of a receipt as read: a product (maybe bought several times) or a standalone line. */
export interface ReceiptRow {
  key: number;
  kind: LineItemOut["kind"];
  /** The product's name when the line is matched, else the line's own description. */
  name: string;
  /** The printed text (without its item code) when it reads differently from `name`. */
  printed: string | null;
  category: Category | null;
  productId: number | null;
  /**
   * How many were bought, as a decimal: the summed quantities of collapsed lines (only whole
   * quantities collapse), or a weighed line's own quantity, e.g. "1.36".
   */
  quantity: string;
  /** What was paid, after the discounts that apply to these lines. */
  paid: MoneyOut;
  /** What those discounts took off (> 0), or null without one. */
  saved: MoneyOut | null;
}

interface Group {
  first: LineItemOut;
  lines: LineItemOut[];
  /** The folded discounts' cents (<= 0). */
  discount: number;
}

const ITEM_CODE = /^\d{3,}\s+/;

/**
 * The receipt's lines as rows to read: a discount that names its item is folded into that
 * item; lines of the same product, or identical unmatched lines one after another, become one
 * "×N" row with summed amounts. Weighed lines (a fractional quantity) never collapse, so each
 * keeps its own quantity. Unlinked discounts, fees and deposits stay rows of their own.
 */
export function receiptRows(items: readonly LineItemOut[]): ReceiptRow[] {
  const byPosition = new Map(items.map((line) => [line.position, line]));
  const target = (line: LineItemOut): number | null =>
    line.kind === "discount" &&
    line.applies_to_position !== null &&
    byPosition.get(line.applies_to_position)?.kind === "item"
      ? line.applies_to_position
      : null;
  const discounts = new Map<number, number>();
  for (const line of items) {
    const position = target(line);
    if (position !== null) {
      discounts.set(
        position,
        (discounts.get(position) ?? 0) + line.line_total.amount_cents,
      );
    }
  }

  const groups: Group[] = [];
  const byProduct = new Map<number, Group>();
  for (const line of items) {
    if (target(line) !== null) continue;
    const discount = discounts.get(line.position) ?? 0;
    const previous = groups.at(-1);
    const joins = !isCountable(line)
      ? undefined
      : line.product_id !== null
        ? byProduct.get(line.product_id)
        : previous && isSameUnmatchedItem(previous.first, line)
          ? previous
          : undefined;
    if (joins) {
      joins.lines.push(line);
      joins.discount += discount;
      continue;
    }
    const group = { first: line, lines: [line], discount };
    groups.push(group);
    if (isCountable(line) && line.product_id !== null) {
      byProduct.set(line.product_id, group);
    }
  }
  return groups.map(toRow);
}

/** An item bought by the piece, which may collapse with others like it. */
function isCountable(line: LineItemOut): boolean {
  return line.kind === "item" && Number.isInteger(Number(line.quantity));
}

function isSameUnmatchedItem(a: LineItemOut, b: LineItemOut): boolean {
  return (
    isCountable(a) &&
    a.product_id === null &&
    b.product_id === null &&
    a.raw_text === b.raw_text &&
    a.description === b.description
  );
}

function printedText(line: LineItemOut): string {
  return line.raw_text.replace(ITEM_CODE, "");
}

function comparable(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
}

function toRow({ first, lines, discount }: Group): ReceiptRow {
  const name = first.product_name ?? (first.description || printedText(first));
  const printed = printedText(first);
  const total = lines.reduce(
    (sum, line) => sum + line.line_total.amount_cents,
    0,
  );
  return {
    key: first.id,
    kind: first.kind,
    name,
    printed: comparable(printed) === comparable(name) ? null : printed,
    category: first.product_category ?? first.category,
    productId: first.product_id,
    quantity: isCountable(first)
      ? String(lines.reduce((sum, line) => sum + Number(line.quantity), 0))
      : first.quantity,
    paid: { ...first.line_total, amount_cents: total + discount },
    saved:
      discount === 0 ? null : { ...first.line_total, amount_cents: -discount },
  };
}
