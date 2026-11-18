import { useState } from "react";

import { cn } from "@/lib/utils";
import type { ProductOut, ProductPurchaseOut } from "@/features/products/api";
import {
  type ChartPoint,
  chartLayout,
  defaultRange,
  inRange,
  rangeFrom,
  RANGES,
  type Range,
} from "@/features/products/price-chart";
import {
  comparablePrices,
  type StoreComparison,
  storeComparison,
  verdictText,
} from "@/features/products/prices";
import { formatMonth, formatShortMonth } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { displayStoreName } from "@/features/receipts/store-name";

const BOX = { width: 320, height: 150, left: 52, right: 8, top: 8, bottom: 22 };
const STYLES = 4;
const DASHES = ["", "6 3", "2 3", "8 3 2 3"];
type Shape = "circle" | "square" | "triangle" | "diamond";
const SHAPES: Shape[] = ["circle", "square", "triangle", "diamond"];

interface StoreStyle {
  colour: string;
  dash: string;
  shape: Shape;
}

/** Colour, dash and marker together, so lines differ by more than colour; after four stores
 * the marker shifts, so no two of the first sixteen look alike. */
function storeStyle(index: number): StoreStyle {
  return {
    colour: `var(--store-${String((index % STYLES) + 1)})`,
    dash: DASHES[index % STYLES] ?? "",
    shape: SHAPES[(index + Math.floor(index / STYLES)) % STYLES] ?? "circle",
  };
}

/**
 * Prices per unit at each store over the chosen range: a chart over time (two dated
 * purchases or more), a table of lowest, average and last per store, and which store is
 * cheaper (two stores or more).
 */
export function PriceHistory({
  product,
  purchases,
}: {
  product: ProductOut;
  purchases: ProductPurchaseOut[];
}) {
  const points: ChartPoint[] = comparablePrices(purchases, product).flatMap(
    ({ item, perUnit }) =>
      item.purchased_on === null
        ? []
        : [
            {
              date: item.purchased_on,
              store: displayStoreName(item.store_name),
              cents: perUnit.amount_cents,
            },
          ],
  );
  const [range, setRange] = useState<Range>(() => defaultRange(points));
  const overall = storeComparison(purchases, product);
  if (overall === null) return null;

  const charted = points.length >= 2;
  const active = charted ? range : "All";
  const newest = purchases[0]?.purchased_on ?? null;
  const from = newest === null ? null : rangeFrom(newest, active);
  const comparison =
    storeComparison(
      from === null
        ? purchases
        : purchases.filter(
            (purchase) =>
              purchase.purchased_on !== null && purchase.purchased_on >= from,
          ),
      product,
    ) ?? overall;
  const order = overall.stores.map(({ store }) => store);
  const styleOf = (store: string) => storeStyle(order.indexOf(store));
  const verdict = verdictText(comparison, active);

  return (
    <section aria-labelledby="prices-heading" className="flex flex-col gap-3">
      <h2 id="prices-heading" className="font-semibold">
        Prices
      </h2>
      {charted ? (
        <div className="flex flex-col gap-2">
          <div role="group" aria-label="Chart range" className="flex gap-2">
            {RANGES.map((option) => (
              <button
                key={option}
                type="button"
                aria-pressed={range === option}
                onClick={() => {
                  setRange(option);
                }}
                className={cn(
                  "h-8 rounded-full border px-3 text-sm",
                  range === option
                    ? "border-primary bg-primary text-primary-foreground"
                    : "bg-card hover:bg-accent",
                )}
              >
                {option}
              </button>
            ))}
          </div>
          <PriceChart
            points={inRange(points, range)}
            styleOf={styleOf}
            currency={purchases[0]?.paid.currency ?? "CAD"}
            per={comparison.per}
          />
        </div>
      ) : null}
      <StoreTable comparison={comparison} styleOf={styleOf} />
      {verdict ? (
        <p className="rounded-lg bg-accent px-3 py-2 text-sm text-accent-foreground">
          {verdict}
        </p>
      ) : null}
    </section>
  );
}

function PriceChart({
  points,
  styleOf,
  currency,
  per,
}: {
  points: ChartPoint[];
  styleOf: (store: string) => StoreStyle;
  currency: string;
  per: string;
}) {
  if (points.length < 2) {
    return (
      <p className="rounded-xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
        Fewer than two purchases in this range.
      </p>
    );
  }
  const layout = chartLayout(points, BOX);
  const money = (amount: number) =>
    formatMoney({ amount_cents: amount, currency });
  const byDate = [...points].sort((a, b) => a.date.localeCompare(b.date));
  const from = formatMonth(byDate[0]?.date ?? "");
  const to = formatMonth(byDate.at(-1)?.date ?? "");
  const perStore = layout.series.map(({ store }) => {
    const cents = byDate
      .filter((point) => point.store === store)
      .map((point) => point.cents);
    const last = money(cents.at(-1) ?? 0);
    return cents.length === 1
      ? `${store}: 1 purchase, ${last}`
      : `${store}: ${String(cents.length)} purchases from ${money(Math.min(...cents))} to ${money(Math.max(...cents))}, last ${last}`;
  });
  const label = [
    `Price per ${per}, ${from === to ? from : `${from} to ${to}`}`,
    ...perStore,
  ].join(". ");

  return (
    <>
      <svg
        viewBox={`0 0 ${String(BOX.width)} ${String(BOX.height)}`}
        role="img"
        aria-label={label}
        className="h-auto w-full rounded-xl border bg-card"
      >
        {layout.yTicks.map((tick) => (
          <g key={tick.y}>
            <line
              x1={BOX.left}
              x2={BOX.width - BOX.right}
              y1={tick.y}
              y2={tick.y}
              stroke="var(--border)"
            />
            <text
              x={BOX.left - 6}
              y={tick.y + 3}
              textAnchor="end"
              fontSize="10"
              fill="var(--muted-foreground)"
            >
              {money(tick.cents)}
            </text>
          </g>
        ))}
        {layout.xTicks.map((tick) => (
          <text
            key={tick.date}
            x={tick.x}
            y={BOX.height - 6}
            textAnchor="middle"
            fontSize="10"
            fill="var(--muted-foreground)"
          >
            {formatShortMonth(tick.date)}
          </text>
        ))}
        {layout.series.map(({ store, points: line }) => {
          const style = styleOf(store);
          const end = line.at(-1);
          return (
            <g key={store} data-store={store}>
              <polyline
                points={line
                  .map(({ x, y }) => `${String(x)},${String(y)}`)
                  .join(" ")}
                fill="none"
                stroke={style.colour}
                strokeWidth="2"
                strokeDasharray={style.dash || undefined}
                strokeLinejoin="round"
              />
              {line.slice(0, -1).map(({ x, y }, index) => (
                <Marker key={index} style={style} x={x} y={y} r={2.5} />
              ))}
              {end ? (
                <Marker style={style} x={end.x} y={end.y} r={4.5} emphasised />
              ) : null}
            </g>
          );
        })}
      </svg>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {layout.series.map(({ store }) => (
          <li key={store} className="flex items-center gap-1.5">
            <StoreKey style={styleOf(store)} />
            {store}
          </li>
        ))}
      </ul>
    </>
  );
}

function Marker({
  style,
  x,
  y,
  r,
  emphasised = false,
}: {
  style: StoreStyle;
  x: number;
  y: number;
  r: number;
  emphasised?: boolean;
}) {
  const paint = {
    fill: style.colour,
    "data-point": "",
    ...(emphasised ? { stroke: "var(--card)", strokeWidth: 1.5 } : {}),
  };
  switch (style.shape) {
    case "circle":
      return <circle cx={x} cy={y} r={r} {...paint} />;
    case "square":
      return (
        <rect x={x - r} y={y - r} width={2 * r} height={2 * r} {...paint} />
      );
    case "triangle":
      return (
        <polygon
          points={`${String(x)},${String(y - 1.2 * r)} ${String(x + 1.1 * r)},${String(y + 0.8 * r)} ${String(x - 1.1 * r)},${String(y + 0.8 * r)}`}
          {...paint}
        />
      );
    case "diamond":
      return (
        <polygon
          points={`${String(x)},${String(y - 1.3 * r)} ${String(x + 1.3 * r)},${String(y)} ${String(x)},${String(y + 1.3 * r)} ${String(x - 1.3 * r)},${String(y)}`}
          {...paint}
        />
      );
  }
}

/** A store's line and marker, as a key beside its name. */
function StoreKey({ style }: { style: StoreStyle }) {
  return (
    <svg
      viewBox="0 0 20 10"
      width="20"
      height="10"
      aria-hidden="true"
      className="shrink-0"
    >
      <line
        x1="0"
        y1="5"
        x2="20"
        y2="5"
        stroke={style.colour}
        strokeWidth="2"
        strokeDasharray={style.dash || undefined}
      />
      <Marker style={style} x={10} y={5} r={3} />
    </svg>
  );
}

const CELL = "px-3 py-2 text-right font-normal";

function StoreTable({
  comparison,
  styleOf,
}: {
  comparison: StoreComparison;
  styleOf: (store: string) => StoreStyle;
}) {
  const compared = comparison.stores.length >= 2;
  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <table className="w-full text-sm">
        <thead className="text-muted-foreground">
          <tr>
            <th scope="col" className={cn(CELL, "text-left")}>
              Per {comparison.per}
            </th>
            <th scope="col" className={CELL}>
              Low
            </th>
            <th scope="col" className={CELL}>
              Avg
            </th>
            <th scope="col" className={CELL}>
              Last
            </th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {comparison.stores.map((price, index) => {
            const cheapest = compared && index === 0;
            return (
              <tr key={price.store} className="border-t">
                <th
                  scope="row"
                  className={cn(
                    CELL,
                    "text-left",
                    cheapest && "font-semibold text-primary",
                  )}
                >
                  <span className="flex items-center gap-2">
                    <StoreKey style={styleOf(price.store)} />
                    {price.store}
                    {cheapest ? (
                      <span className="sr-only"> (cheapest)</span>
                    ) : null}
                  </span>
                </th>
                <td className={CELL}>{formatMoney(price.lowest)}</td>
                <td className={CELL}>{formatMoney(price.average)}</td>
                <td className={CELL}>{formatMoney(price.last)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
