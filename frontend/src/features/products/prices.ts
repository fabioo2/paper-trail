import type { ProductPurchaseOut } from "@/features/products/api";
import type { MoneyOut } from "@/features/receipts/money";

export type LineUnit = ProductPurchaseOut["unit"];

/** One pound in kilograms, exactly (the international pound). */
export const POUND_IN_KG = 0.45359237;

/** What a price is per: "/kg", "/lb", or nothing for each (and unknown, compared as each). */
export function unitSuffix(unit: LineUnit): string {
  return unit === "kg" || unit === "lb" ? `/${unit}` : "";
}

function weighed(unit: LineUnit): unit is "kg" | "lb" {
  return unit === "kg" || unit === "lb";
}

/** Same unit as `unit` once a pound is converted to kilograms; unknown counts as each. */
function comparable(from: LineUnit, to: LineUnit): boolean {
  return weighed(from) === weighed(to);
}

/** Cents paid per one `unit` (unrounded): a pound price converted when `unit` is kg, and back. */
export function paidPer(purchase: ProductPurchaseOut, unit: LineUnit): number {
  const perOwnUnit = purchase.paid.amount_cents / Number(purchase.quantity);
  if (purchase.unit === "lb" && unit === "kg") return perOwnUnit / POUND_IN_KG;
  if (purchase.unit === "kg" && unit === "lb") return perOwnUnit * POUND_IN_KG;
  return perOwnUnit;
}

export interface UnitPriceSummary {
  lowest: MoneyOut;
  average: MoneyOut;
  unit: LineUnit;
  /** How many purchases were compared, the newest included. */
  count: number;
}

/**
 * The lowest and average price paid per unit, over the purchases in the newest purchase's
 * currency and unit (pounds and kilograms compared after conversion), in the newest purchase's
 * unit. Rounded to the cent only at the end. Null without purchases.
 */
export function unitPriceSummary(
  purchases: ProductPurchaseOut[],
): UnitPriceSummary | null {
  const newest = purchases[0];
  if (newest === undefined) return null;
  const perUnit = purchases
    .filter(
      (purchase) =>
        purchase.paid.currency === newest.paid.currency &&
        comparable(purchase.unit, newest.unit),
    )
    .map((purchase) => paidPer(purchase, newest.unit));
  const cents = (value: number): MoneyOut => ({
    amount_cents: Math.round(value),
    currency: newest.paid.currency,
  });
  return {
    lowest: cents(Math.min(...perUnit)),
    average: cents(
      perUnit.reduce((sum, value) => sum + value, 0) / perUnit.length,
    ),
    unit: newest.unit,
    count: perUnit.length,
  };
}
