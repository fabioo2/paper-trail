import { describe, expect, it } from "vitest";

import {
  type ChartPoint,
  chartLayout,
  defaultRange,
  inRange,
  type Range,
  rangeFrom,
  sparklinePoints,
} from "@/features/products/price-chart";

describe("sparklinePoints", () => {
  it.each<[string, number[], [number, number][]]>([
    ["no values", [], []],
    ["one value is no trend", [899], []],
    [
      "two values span the width, the higher on top",
      [200, 100],
      [
        [2, 2],
        [52, 18],
      ],
    ],
    [
      "a flat series runs through the middle",
      [500, 500, 500],
      [
        [2, 10],
        [27, 10],
        [52, 10],
      ],
    ],
    [
      "evenly spaced, scaled between the paddings",
      [100, 150, 200],
      [
        [2, 18],
        [27, 10],
        [52, 2],
      ],
    ],
  ])("%s", (_, values, expected) => {
    expect(sparklinePoints(values, 54, 20).map(({ x, y }) => [x, y])).toEqual(
      expected,
    );
  });
});

function at(date: string, cents = 100, store = "Costco"): ChartPoint {
  return { date, store, cents };
}

describe("rangeFrom", () => {
  it.each<[string, Range, string | null]>([
    ["2026-11-15", "1M", "2026-10-15"],
    ["2026-11-15", "6M", "2026-05-15"],
    ["2026-03-31", "1M", "2026-02-28"],
    ["2028-03-31", "1M", "2028-02-29"],
    ["2026-05-31", "1M", "2026-04-30"],
    ["2026-08-31", "6M", "2026-02-28"],
    ["2028-02-29", "6M", "2027-08-29"],
    ["2026-01-31", "1M", "2025-12-31"],
    ["2026-03-15", "6M", "2025-09-15"],
    ["2026-11-15", "All", null],
  ])("%s back %s is %s", (newest, range, from) => {
    expect(rangeFrom(newest, range)).toBe(from);
  });
});

describe("ranges", () => {
  const points = [
    at("2026-11-15"),
    at("2026-10-20"),
    at("2026-10-15"),
    at("2026-06-01"),
    at("2026-01-10"),
  ];

  it.each([
    ["1M", ["2026-11-15", "2026-10-20", "2026-10-15"]],
    ["6M", ["2026-11-15", "2026-10-20", "2026-10-15", "2026-06-01"]],
    ["All", points.map(({ date }) => date)],
  ] as const)(
    "%s counts back from the newest purchase, its first day included",
    (range, dates) => {
      expect(inRange(points, range).map(({ date }) => date)).toEqual(dates);
    },
  );

  it.each<[string, ChartPoint[], string]>([
    ["more than six months of history", points, "6M"],
    [
      "everything is within six months",
      [at("2026-11-15"), at("2026-06-01")],
      "All",
    ],
    [
      "six months would leave one point",
      [at("2026-11-15"), at("2026-01-10"), at("2025-12-01")],
      "All",
    ],
  ])("defaults to the right range when %s", (_, history, range) => {
    expect(defaultRange(history)).toBe(range);
  });
});

describe("chartLayout", () => {
  const box = {
    width: 120,
    height: 70,
    left: 10,
    right: 10,
    top: 10,
    bottom: 10,
  };

  it("puts time across and price up, one line per store, months on the axis", () => {
    const layout = chartLayout(
      [
        at("2026-03-01", 200, "Metro"),
        at("2026-01-01", 100, "Costco"),
        at("2026-02-01", 300, "Costco"),
      ],
      box,
    );

    expect(layout).toEqual({
      series: [
        {
          store: "Costco",
          points: [
            { x: 10, y: 60 },
            { x: 10 + (31 / 59) * 100, y: 10 },
          ],
        },
        { store: "Metro", points: [{ x: 110, y: 35 }] },
      ],
      yTicks: [
        { y: 10, cents: 300 },
        { y: 35, cents: 200 },
        { y: 60, cents: 100 },
      ],
      xTicks: [
        { x: 10, date: "2026-01-01" },
        { x: 10 + (31 / 59) * 100, date: "2026-02-01" },
        { x: 110, date: "2026-03-01" },
      ],
    });
  });

  it("centres one day and pads a flat price", () => {
    const layout = chartLayout(
      [at("2026-01-15", 500, "Costco"), at("2026-01-15", 500, "Metro")],
      box,
    );

    expect(layout.series.map(({ points }) => points)).toEqual([
      [{ x: 60, y: 35 }],
      [{ x: 60, y: 35 }],
    ]);
    expect(layout.yTicks.map(({ cents }) => cents)).toEqual([550, 500, 450]);
    expect(layout.xTicks).toEqual([{ x: 60, date: "2026-01-15" }]);
  });

  it("labels at most four months", () => {
    const layout = chartLayout([at("2025-01-01"), at("2026-01-01")], box);

    expect(layout.xTicks.map(({ date }) => date)).toEqual([
      "2025-01-01",
      "2025-05-01",
      "2025-09-01",
      "2026-01-01",
    ]);
  });
});
