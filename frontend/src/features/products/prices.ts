import type { ProductOut, ProductPurchaseOut } from "@/features/products/api";
import type { Range } from "@/features/products/price-chart";
import type { MoneyOut } from "@/features/receipts/money";
import { displayStoreName } from "@/features/receipts/store-name";

export type LineUnit = ProductPurchaseOut["unit"];

/** What a price per unit needs: what was paid for how much of what (a purchase or a point). */
export type Priced = Pick<ProductPurchaseOut, "paid" | "quantity" | "unit">;

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

/** A decimal string such as "0.452" (or "4.52E-1") as an exact fraction. */
function decimal(text: string): Cents {
  const [mantissa = "0", exponent = "0"] = text.toLowerCase().split("e");
  const [whole = "0", fraction = ""] = mantissa.split(".");
  const shift = Number(exponent) - fraction.length;
  const digits = BigInt(whole + fraction);
  return shift >= 0
    ? { num: digits * 10n ** BigInt(shift), den: 1n }
    : { num: digits, den: 10n ** BigInt(-shift) };
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
function paidPer(purchase: Priced, unit: LineUnit): Cents {
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
export function paidPerUnit(purchase: Priced): MoneyOut {
  return {
    amount_cents: rounded(paidPer(purchase, purchase.unit)),
    currency: purchase.paid.currency,
  };
}

/** The purchases in `newest`'s currency and unit (pounds and kilograms compared). */
function comparableWith<T extends Priced>(purchases: T[], newest: Priced): T[] {
  return purchases.filter(
    (purchase) =>
      purchase.paid.currency === newest.paid.currency &&
      comparable(purchase.unit, newest.unit),
  );
}

function lowestOf(values: Cents[]): Cents {
  return values.reduce((low, value) => (compare(value, low) < 0 ? value : low));
}

function averageOf(values: Cents[]): Cents {
  const total = values.reduce((sum, value) => ({
    num: sum.num * value.den + value.num * sum.den,
    den: sum.den * value.den,
  }));
  return { num: total.num, den: total.den * BigInt(values.length) };
}

/** `(a - b) / b` as a fraction (its sign in `num`); `b` must not be zero. */
function relative(a: Cents, b: Cents): Cents {
  return { num: a.num * b.den - b.num * a.den, den: b.num * a.den };
}

/** A fraction as a whole percentage without its sign, rounded half up. */
function percentOf(ratio: Cents): number {
  const num = ratio.num < 0n ? -ratio.num : ratio.num;
  return rounded({ num: num * 100n, den: ratio.den });
}

export interface PricedItem<T> {
  item: T;
  /** What one of the newest purchase's unit cost, to the cent. */
  perUnit: MoneyOut;
}

/**
 * The purchases (newest first) comparable with the newest one, in the same order, each with
 * its price per the newest one's unit (by the product's size when it has one, see `basisFor`):
 * the series a trend line draws.
 */
export function comparablePrices<T extends Priced>(
  purchases: T[],
  product: Sized | null = null,
): PricedItem<T>[] {
  const newest = purchases[0];
  if (newest === undefined) return [];
  const { scale } = basisFor(product, newest.unit);
  return comparableWith(purchases, newest).map((item) => ({
    item,
    perUnit: {
      amount_cents: rounded(scaled(paidPer(item, newest.unit), scale)),
      currency: newest.paid.currency,
    },
  }));
}

/** Within this many percent of the earlier average (inclusive), a price is steady. */
export const STEADY_PERCENT = 2;

export type PriceChange =
  { direction: "steady" } | { direction: "up" | "down"; percent: number };

/**
 * The newest purchase's price per unit against the average of the earlier comparable ones
 * (newest first): steady within ±2 %, else up or down by a whole percentage. Null with no
 * earlier comparable purchase, or when they were free.
 */
export function priceChange(purchases: Priced[]): PriceChange | null {
  const [newest, ...earlier] = purchases;
  if (newest === undefined) return null;
  const before = comparableWith(earlier, newest).map((purchase) =>
    paidPer(purchase, newest.unit),
  );
  if (before.length === 0) return null;
  const average = averageOf(before);
  if (average.num === 0n) return null;
  const change = relative(paidPer(newest, newest.unit), average);
  const magnitude = change.num < 0n ? -change.num : change.num;
  if (magnitude * 100n <= BigInt(STEADY_PERCENT) * change.den) {
    return { direction: "steady" };
  }
  return {
    direction: change.num < 0n ? "down" : "up",
    percent: percentOf(change),
  };
}

export interface StorePrice {
  /** The store as people say it (`displayStoreName`): a chain's branches are one store. */
  store: string;
  lowest: MoneyOut;
  average: MoneyOut;
  /** The store's newest comparable purchase. */
  last: MoneyOut;
  count: number;
}

export interface StoreComparison {
  /** Cheapest average first, then by name. */
  stores: StorePrice[];
  /** What the prices are per: "kg", "lb", "L", "100 g" or "item" (`basisFor`). */
  per: string;
  /** How many purchases were compared, over every store. */
  count: number;
  /** The cheapest store against the next cheapest (`equal`: the same average), with two
   * stores or more. */
  cheaper: {
    store: string;
    than: string;
    percent: number;
    equal: boolean;
  } | null;
}

/**
 * Per store, the lowest, average and last price per unit of the purchases (newest first) in
 * the newest purchase's currency and unit (pounds and kilograms compared after conversion), in
 * the newest purchase's unit, or by the product's size (`basisFor`). Exact until the final
 * rounding, half up, to the cent. Null without purchases.
 */
export function storeComparison(
  purchases: ProductPurchaseOut[],
  product: Sized | null = null,
): StoreComparison | null {
  const newest = purchases[0];
  if (newest === undefined) return null;
  const basis = basisFor(product, newest.unit);
  const byStore = new Map<string, Cents[]>();
  for (const purchase of comparableWith(purchases, newest)) {
    const store = displayStoreName(purchase.store_name);
    byStore.set(store, [
      ...(byStore.get(store) ?? []),
      scaled(paidPer(purchase, newest.unit), basis.scale),
    ]);
  }
  const cents = (value: Cents): MoneyOut => ({
    amount_cents: rounded(value),
    currency: newest.paid.currency,
  });
  const ranked = [...byStore]
    .map(([store, values]) => ({ store, values, average: averageOf(values) }))
    .sort(
      (a, b) => compare(a.average, b.average) || a.store.localeCompare(b.store),
    );
  const [best, next] = ranked;
  return {
    stores: ranked.map(({ store, values, average }) => ({
      store,
      lowest: cents(lowestOf(values)),
      average: cents(average),
      last: cents(values[0] ?? average),
      count: values.length,
    })),
    per: basis.per,
    count: ranked.reduce((total, { values }) => total + values.length, 0),
    cheaper:
      best && next && next.average.num > 0n
        ? {
            store: best.store,
            than: next.store,
            percent: percentOf(relative(best.average, next.average)),
            equal: compare(best.average, next.average) === 0,
          }
        : null,
  };
}

const PERIOD: Record<Range, string | null> = {
  "1M": "in the last month",
  "6M": "in the last 6 months",
  All: null,
};

/**
 * One line naming the cheapest store, e.g. "Costco is 6% cheaper per kg than Metro across
 * your last 4 purchases" (or "in the last month" for that range); "Last time, …" when each
 * store was bought at once; "about the same" under half a percent apart. Null with fewer
 * than two stores.
 */
export function verdictText(
  comparison: StoreComparison,
  range: Range = "All",
): string | null {
  const { cheaper, per, count, stores } = comparison;
  if (cheaper === null) return null;
  const { store, than, percent, equal } = cheaper;
  const same = equal ? "the same" : "about the same";
  if (stores.every((price) => price.count === 1)) {
    return percent === 0
      ? `Last time, ${store} and ${than} cost ${same} per ${per}`
      : `Last time, ${store} was ${String(percent)}% cheaper per ${per} than ${than}`;
  }
  const period = PERIOD[range] ?? `across your last ${String(count)} purchases`;
  return percent === 0
    ? `${store} and ${than} cost ${same} per ${per} ${period}`
    : `${store} is ${String(percent)}% cheaper per ${per} than ${than} ${period}`;
}

/** A size unit in kilograms or litres. */
const IN_BASE_UNITS: Partial<
  Record<NonNullable<ProductOut["size_unit"]>, Cents>
> = {
  g: { num: 1n, den: 1000n },
  kg: { num: 1n, den: 1n },
  lb: POUND_IN_KG,
  oz: { num: POUND_IN_KG.num, den: POUND_IN_KG.den * 16n },
  ml: { num: 1n, den: 1000n },
  l: { num: 1n, den: 1n },
};

type Sized = Pick<ProductOut, "size_value" | "size_unit">;

const ONE: Cents = { num: 1n, den: 1n };

function scaled(value: Cents, scale: Cents): Cents {
  return { num: value.num * scale.num, den: value.den * scale.den };
}

interface Basis {
  /** "kg", "lb", "L", "100 g" or "item". */
  per: string;
  /** What a price per `unit` is multiplied by to be per `per`. */
  scale: Cents;
}

/**
 * What a price per `unit` is best said per: a weighed unit as itself; an item by the
 * product's size, per litre, per kilogram, or per 100 g for a package under a kilogram; else
 * per item (no size, a size in items, a zero size).
 */
function basisFor(product: Sized | null, unit: LineUnit): Basis {
  if (weighed(unit)) return { per: unit, scale: ONE };
  const base =
    product?.size_unit == null ? undefined : IN_BASE_UNITS[product.size_unit];
  if (base === undefined || product?.size_value == null) {
    return { per: "item", scale: ONE };
  }
  const size = decimal(product.size_value);
  const packageSize = { num: size.num * base.num, den: size.den * base.den };
  if (packageSize.num === 0n) return { per: "item", scale: ONE };
  const volume = product.size_unit === "ml" || product.size_unit === "l";
  const underAKilo = !volume && packageSize.num < packageSize.den;
  return {
    per: volume ? "L" : underAKilo ? "100 g" : "kg",
    scale: {
      num: packageSize.den,
      den: packageSize.num * (underAKilo ? 10n : 1n),
    },
  };
}

export interface SizePrice {
  price: MoneyOut;
  /** "/kg", "/lb", "/100 g" (a package under a kilogram) or "/L". */
  suffix: string;
}

/**
 * What a purchase cost by weight or volume (`basisFor`): a weighed line's own price per kg or
 * lb, else a package's price over the product's size. Null when the size doesn't tell.
 */
export function sizePrice(product: Sized, purchase: Priced): SizePrice | null {
  const basis = basisFor(product, purchase.unit);
  if (basis.per === "item" || decimal(purchase.quantity).num === 0n) {
    return null;
  }
  return {
    price: {
      amount_cents: rounded(
        scaled(paidPer(purchase, purchase.unit), basis.scale),
      ),
      currency: purchase.paid.currency,
    },
    suffix: `/${basis.per}`,
  };
}
