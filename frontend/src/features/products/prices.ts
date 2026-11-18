import type { ProductPurchaseOut } from "@/features/products/api";
import type { MoneyOut } from "@/features/receipts/money";

export type LineUnit = ProductPurchaseOut["unit"];

/** One pound in kilograms, exactly (the international pound): 0.45359237. */
const POUND_IN_KG = { num: 45_359_237n, den: 100_000_000n };

/** An exact non-negative fraction of cents; prices are compared and summed without floats. */
interface Cents {
  num: bigint;
  den: bigint;
}

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

/** A decimal string such as "0.452" as an exact fraction. */
function decimal(text: string): Cents {
  const [whole = "0", fraction = ""] = text.split(".");
  return {
    num: BigInt(whole + fraction),
    den: 10n ** BigInt(fraction.length),
  };
}

function compare(a: Cents, b: Cents): number {
  const left = a.num * b.den;
  const right = b.num * a.den;
  return left < right ? -1 : left > right ? 1 : 0;
}

/** Rounded half up to a whole cent. */
function rounded(value: Cents): number {
  return Number((2n * value.num + value.den) / (2n * value.den));
}

/** Cents paid per one `unit`, exactly: a pound price converted when `unit` is kg, and back. */
function paidPer(purchase: ProductPurchaseOut, unit: LineUnit): Cents {
  const quantity = decimal(purchase.quantity);
  let num = BigInt(purchase.paid.amount_cents) * quantity.den;
  let den = quantity.num;
  if (purchase.unit === "lb" && unit === "kg") {
    num *= POUND_IN_KG.den;
    den *= POUND_IN_KG.num;
  } else if (purchase.unit === "kg" && unit === "lb") {
    num *= POUND_IN_KG.num;
    den *= POUND_IN_KG.den;
  }
  return { num, den };
}

/** What one unit of the purchase cost, in its own unit, to the cent. */
export function paidPerUnit(purchase: ProductPurchaseOut): MoneyOut {
  return {
    amount_cents: rounded(paidPer(purchase, purchase.unit)),
    currency: purchase.paid.currency,
  };
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
 * unit. Exact until the final rounding, half up, to the cent. Null without purchases.
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
  const lowest = perUnit.reduce((low, value) =>
    compare(value, low) < 0 ? value : low,
  );
  const total = perUnit.reduce((sum, value) => ({
    num: sum.num * value.den + value.num * sum.den,
    den: sum.den * value.den,
  }));
  const cents = (value: Cents): MoneyOut => ({
    amount_cents: rounded(value),
    currency: newest.paid.currency,
  });
  return {
    lowest: cents(lowest),
    average: cents({
      num: total.num,
      den: total.den * BigInt(perUnit.length),
    }),
    unit: newest.unit,
    count: perUnit.length,
  };
}
