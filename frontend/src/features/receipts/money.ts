import type { components } from "@/api/schema";

export type MoneyOut = components["schemas"]["MoneyOut"];

const CENTS_PER_UNIT = 100;

/**
 * The amount in its own currency, e.g. `-$2.50` in en-CA: the one money formatter of the app.
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
  }).format(decimal);
}
