import type { components } from "@/api/schema";

export type MoneyOut = components["schemas"]["MoneyOut"];

const CENTS_PER_UNIT = 100;
/** The app's own currency, shown as a bare "$" in every locale, never "CA$". */
const HOME_CURRENCY = "CAD";

/**
 * The amount in its own currency, e.g. `-$2.50` in en-CA: the one money formatter of the app.
 * Canadian dollars are a plain "$" whatever the locale; any other currency keeps its marker
 * ("US$", "€").
 * Cents are turned into an exact decimal string (never a float divided by 100), which
 * `Intl.NumberFormat` formats without binary rounding.
 */
export function formatMoney(money: MoneyOut, locale?: string): string {
  const sign = money.amount_cents < 0 ? "-" : "";
  const cents = Math.abs(money.amount_cents);
  const units = Math.trunc(cents / CENTS_PER_UNIT);
  const fraction = String(cents % CENTS_PER_UNIT).padStart(2, "0");
  // An optional minus, digits, a point and two digits: a numeric literal by construction.
  const decimal = `${sign}${units}.${fraction}` as Intl.StringNumericLiteral;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: money.currency,
    currencyDisplay:
      money.currency === HOME_CURRENCY ? "narrowSymbol" : "symbol",
  }).format(decimal);
}
