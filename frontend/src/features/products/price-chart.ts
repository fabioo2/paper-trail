export interface Point {
  x: number;
  y: number;
}

/**
 * A sparkline's points for `values` (oldest first), evenly spaced across `width` and scaled
 * between `pad` and `height - pad`, the highest value on top; a flat series runs through the
 * middle. Empty below two values: one price is no trend.
 */
export function sparklinePoints(
  values: number[],
  width: number,
  height: number,
  pad = 2,
): Point[] {
  if (values.length < 2) return [];
  const low = Math.min(...values);
  const high = Math.max(...values);
  const step = (width - 2 * pad) / (values.length - 1);
  return values.map((value, index) => ({
    x: pad + index * step,
    y:
      high === low
        ? height / 2
        : pad + ((high - value) / (high - low)) * (height - 2 * pad),
  }));
}

export interface ChartPoint {
  /** `YYYY-MM-DD` */
  date: string;
  store: string;
  cents: number;
}

export const RANGES = ["1M", "6M", "All"] as const;
export type Range = (typeof RANGES)[number];

const RANGE_MONTHS: Record<Range, number | null> = {
  "1M": 1,
  "6M": 6,
  All: null,
};

const DAY_MS = 86_400_000;

function day(isoDate: string): number {
  return Date.parse(`${isoDate}T00:00:00Z`) / DAY_MS;
}

/**
 * The first day of `range` ending on `newest`: the same day `range` months earlier, or that
 * month's last day when it is shorter (May 31 - 1M is Apr 30); null for everything.
 */
export function rangeFrom(newest: string, range: Range): string | null {
  const months = RANGE_MONTHS[range];
  if (months === null) return null;
  const [year = 0, month = 1, date = 1] = newest.split("-").map(Number);
  const first = new Date(Date.UTC(year, month - 1 - months, 1));
  const lastDay = new Date(
    Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0),
  ).getUTCDate();
  first.setUTCDate(Math.min(date, lastDay));
  return first.toISOString().slice(0, 10);
}

function newestDate(points: ChartPoint[]): string {
  return points.reduce(
    (newest, point) => (point.date > newest ? point.date : newest),
    "",
  );
}

/** The points within `range` before the newest one (its own date counted from). */
export function inRange(points: ChartPoint[], range: Range): ChartPoint[] {
  if (points.length === 0) return points;
  const from = rangeFrom(newestDate(points), range);
  return from === null ? points : points.filter((point) => point.date >= from);
}

/** Six months when there is more history than that and they hold a line; else everything. */
export function defaultRange(points: ChartPoint[]): Range {
  const recent = inRange(points, "6M");
  return recent.length < points.length && recent.length >= 2 ? "6M" : "All";
}

export interface ChartBox {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export interface ChartLayout {
  /** One line per store, in the order the stores first appear, points by date. */
  series: { store: string; points: Point[] }[];
  /** Highest first. */
  yTicks: { y: number; cents: number }[];
  /** The first of each month in the range (at most four), or the first point's month. */
  xTicks: { x: number; date: string }[];
}

const MAX_X_TICKS = 4;

/** Where each price goes in `box`: time across, price up, a flat price in the middle. */
export function chartLayout(points: ChartPoint[], box: ChartBox): ChartLayout {
  const days = points.map((point) => day(point.date));
  const first = Math.min(...days);
  const last = Math.max(...days);
  const cents = points.map((point) => point.cents);
  const min = Math.min(...cents);
  const max = Math.max(...cents);
  const spread = min === max ? Math.max(1, Math.round(min * 0.1)) : 0;
  const low = min - spread;
  const high = max + spread;
  const plotWidth = box.width - box.left - box.right;
  const plotHeight = box.height - box.top - box.bottom;
  const x = (at: number) =>
    first === last
      ? box.left + plotWidth / 2
      : box.left + ((at - first) / (last - first)) * plotWidth;
  const y = (value: number) =>
    box.top + ((high - value) / (high - low)) * plotHeight;

  const series = new Map<string, Point[]>();
  [...points]
    .sort((a, b) => a.date.localeCompare(b.date))
    .forEach((point) => {
      series.set(point.store, [
        ...(series.get(point.store) ?? []),
        { x: x(day(point.date)), y: y(point.cents) },
      ]);
    });

  const middle = Math.round((low + high) / 2);
  const firstDate = new Date(first * DAY_MS).toISOString().slice(0, 10);
  const months: string[] = [];
  const month = new Date(`${firstDate.slice(0, 7)}-01T00:00:00Z`);
  while (day(month.toISOString().slice(0, 10)) <= last) {
    const iso = month.toISOString().slice(0, 10);
    if (day(iso) >= first) months.push(iso);
    month.setUTCMonth(month.getUTCMonth() + 1);
  }
  const step = Math.ceil(months.length / MAX_X_TICKS);
  const ticks = months.filter((_, index) => index % step === 0);

  return {
    series: [...series].map(([store, line]) => ({ store, points: line })),
    yTicks: [high, middle, low].map((value) => ({ y: y(value), cents: value })),
    xTicks: (ticks.length > 0 ? ticks : [firstDate]).map((date) => ({
      x: x(Math.max(day(date), first)),
      date,
    })),
  };
}
