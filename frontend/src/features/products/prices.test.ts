import { describe, expect, it } from "vitest";

import type { Range } from "@/features/products/price-chart";
import {
  comparablePrices,
  type PriceChange,
  priceChange,
  sizePrice,
  type StoreComparison,
  storeComparison,
  verdictText,
} from "@/features/products/prices";
import {
  product,
  productPurchase,
  type ProductPurchaseOut,
} from "@/test/fixtures/products";
import { cad } from "@/test/fixtures/receipts";

const COSTCO = "COSTCO WHOLESALE #1234";
const METRO = "METRO #77";

function bought(
  paid: number,
  quantity: string,
  unit: ProductPurchaseOut["unit"],
  store_name = COSTCO,
): ProductPurchaseOut {
  return productPurchase({ paid: cad(paid), quantity, unit, store_name });
}

function usd(paid: number): ProductPurchaseOut {
  return productPurchase({ paid: { amount_cents: paid, currency: "USD" } });
}

describe("storeComparison at one store: the exact unit maths", () => {
  it.each<{
    name: string;
    purchases: ProductPurchaseOut[];
    expected: [number, number, number, string, number];
  }>([
    {
      name: "a newest purchase alone is lowest, average and last",
      purchases: [bought(441, "1", "kg")],
      expected: [441, 441, 441, "kg", 1],
    },
    {
      name: "another currency is left out",
      purchases: [bought(899, "1", "each"), usd(100)],
      expected: [899, 899, 899, "item", 1],
    },
    {
      name: "each and kg are not compared",
      purchases: [bought(899, "1", "each"), bought(100, "1", "kg")],
      expected: [899, 899, 899, "item", 1],
    },
    {
      name: "an unknown unit counts as each",
      purchases: [bought(899, "1", "each"), bought(1299, "1", null)],
      expected: [899, 1099, 899, "item", 2],
    },
    {
      name: "pounds are compared as kilograms: $2.00/lb is $4.41/kg",
      purchases: [bought(500, "1", "kg"), bought(200, "1", "lb")],
      expected: [441, 470, 500, "kg", 2],
    },
    {
      name: "kilograms are compared as pounds when the newest is in pounds",
      purchases: [bought(200, "1", "lb"), bought(441, "1", "kg")],
      expected: [200, 200, 200, "lb", 2],
    },
    {
      name: "a converted price of exactly half a cent rounds up: 0.90718474 kg for $2.39 is $1.195/lb",
      purchases: [bought(300, "1", "lb"), bought(239, "0.90718474", "kg")],
      expected: [120, 210, 300, "lb", 2],
    },
    {
      name: "a quantity in exponent notation is read exactly",
      purchases: [bought(452, "4.52E-1", "kg"), bought(1000, "1E+1", "kg")],
      expected: [100, 550, 1000, "kg", 2],
    },
    {
      name: "full precision is kept until the final rounding",
      purchases: [bought(1001, "10", "each"), bought(503, "5", "each")],
      expected: [100, 100, 100, "item", 2],
    },
  ])(
    "$name",
    ({ purchases, expected: [lowest, average, last, per, count] }) => {
      expect(storeComparison(purchases)).toEqual({
        stores: [
          {
            store: "Costco",
            lowest: cad(lowest),
            average: cad(average),
            last: cad(last),
            count,
          },
        ],
        per,
        count,
        cheaper: null,
      });
    },
  );

  it("is null without purchases", () => {
    expect(storeComparison([])).toBeNull();
  });
});

describe("storeComparison across stores", () => {
  it("ranks stores by average, merges a chain's branches, and names the cheapest against the next", () => {
    const comparison = storeComparison([
      bought(470, "1", "kg", METRO),
      bought(450, "1", "kg", "COSTCO WHOLESALE #9"),
      bought(440, "1", "kg", COSTCO),
      bought(530, "1", "kg", METRO),
      bought(600, "1", "kg", "FARM BOY"),
    ]);

    expect(comparison).toEqual({
      stores: [
        {
          store: "Costco",
          lowest: cad(440),
          average: cad(445),
          last: cad(450),
          count: 2,
        },
        {
          store: "Metro",
          lowest: cad(470),
          average: cad(500),
          last: cad(470),
          count: 2,
        },
        {
          store: "Farm Boy",
          lowest: cad(600),
          average: cad(600),
          last: cad(600),
          count: 1,
        },
      ],
      per: "kg",
      count: 5,
      cheaper: { store: "Costco", than: "Metro", percent: 11, equal: false },
    });
  });

  it("prices packages by the product's size, per litre", () => {
    const comparison = storeComparison(
      [bought(609, "1", "each"), bought(1300, "2", null, METRO)],
      product({ size_value: "4", size_unit: "l" }),
    );

    expect(comparison).toEqual({
      stores: [
        {
          store: "Costco",
          lowest: cad(152),
          average: cad(152),
          last: cad(152),
          count: 1,
        },
        {
          store: "Metro",
          lowest: cad(163),
          average: cad(163),
          last: cad(163),
          count: 1,
        },
      ],
      per: "L",
      count: 2,
      cheaper: { store: "Costco", than: "Metro", percent: 6, equal: false },
    });
  });

  it.each([
    ["the same average is equal", 500, true],
    ["a cent apart rounds to 0% but is not equal", 501, false],
  ])("%s", (_, metro, equal) => {
    expect(
      storeComparison([bought(500, "1", "kg"), bought(metro, "1", "kg", METRO)])
        ?.cheaper,
    ).toEqual({ store: "Costco", than: "Metro", percent: 0, equal });
  });

  it("leaves out a store whose purchases are in another unit", () => {
    const comparison = storeComparison([
      bought(470, "1", "kg", METRO),
      bought(899, "1", "each", COSTCO),
    ]);

    expect(comparison?.stores.map(({ store }) => store)).toEqual(["Metro"]);
    expect(comparison?.cheaper).toBeNull();
  });
});

describe("verdictText", () => {
  const twice = { lowest: cad(1), average: cad(1), last: cad(1), count: 2 };
  const base: StoreComparison = {
    stores: [
      { ...twice, store: "Costco" },
      { ...twice, store: "Metro" },
    ],
    per: "kg",
    count: 4,
    cheaper: { store: "Costco", than: "Metro", percent: 6, equal: false },
  };
  const once: StoreComparison = {
    ...base,
    stores: base.stores.map((price) => ({ ...price, count: 1 })),
    count: 2,
  };
  const same = { store: "Costco", than: "Metro", percent: 0 };

  it.each<[string, StoreComparison, Range, string | null]>([
    ["one store: no verdict", { ...base, cheaper: null }, "All", null],
    [
      "a cheaper store, over every purchase",
      base,
      "All",
      "Costco is 6% cheaper per kg than Metro across your last 4 purchases",
    ],
    [
      "in the last month",
      base,
      "1M",
      "Costco is 6% cheaper per kg than Metro in the last month",
    ],
    [
      "in the last six months, per litre",
      { ...base, per: "L" },
      "6M",
      "Costco is 6% cheaper per L than Metro in the last 6 months",
    ],
    [
      "under half a percent apart",
      { ...base, cheaper: { ...same, equal: false } },
      "All",
      "Costco and Metro cost about the same per kg across your last 4 purchases",
    ],
    [
      "exactly equal",
      { ...base, cheaper: { ...same, equal: true } },
      "All",
      "Costco and Metro cost the same per kg across your last 4 purchases",
    ],
    [
      "one purchase per store: what happened last time",
      once,
      "All",
      "Last time, Costco was 6% cheaper per kg than Metro",
    ],
    [
      "one purchase per store, about the same",
      { ...once, cheaper: { ...same, equal: false } },
      "1M",
      "Last time, Costco and Metro cost about the same per kg",
    ],
  ])("%s", (_, comparison, range, expected) => {
    expect(verdictText(comparison, range)).toBe(expected);
  });
});

describe("priceChange", () => {
  it.each<[string, ProductPurchaseOut[], PriceChange | null]>([
    ["no purchases", [], null],
    ["one purchase: no change to show", [bought(899, "1", null)], null],
    [
      "against the average of every earlier purchase",
      [
        bought(1199, "1", null),
        bought(1599, "1", null),
        bought(1599, "1", null),
      ],
      { direction: "down", percent: 25 },
    ],
    [
      "dearer",
      [bought(168, "1", null), bought(155, "1", null)],
      { direction: "up", percent: 8 },
    ],
    [
      "exactly 2% is still steady",
      [bought(1020, "1", null), bought(1000, "1", null)],
      { direction: "steady" },
    ],
    [
      "just over 2% is a change",
      [bought(1021, "1", null), bought(1000, "1", null)],
      { direction: "up", percent: 2 },
    ],
    [
      "-2% is steady",
      [bought(980, "1", null), bought(1000, "1", null)],
      { direction: "steady" },
    ],
    [
      "per unit, not per line: 2 for $10 after 1 for $5 is steady",
      [bought(1000, "2", "each"), bought(500, "1", "each")],
      { direction: "steady" },
    ],
    [
      "pounds against kilograms after conversion",
      [bought(200, "1", "lb"), bought(441, "1", "kg")],
      { direction: "steady" },
    ],
    [
      "earlier purchases in another unit don't count",
      [bought(899, "1", "each"), bought(100, "1", "kg")],
      null,
    ],
    [
      "another currency doesn't count",
      [bought(899, "1", null), usd(100)],
      null,
    ],
    [
      "free earlier purchases give no percentage",
      [bought(899, "1", null), bought(0, "1", null)],
      null,
    ],
  ])("%s", (_, purchases, expected) => {
    expect(priceChange(purchases)).toEqual(expected);
  });
});

describe("comparablePrices", () => {
  it("keeps the newest one's currency and unit, in order, priced per its unit", () => {
    const purchases = [
      bought(500, "1", "kg"),
      bought(899, "1", "each"),
      bought(200, "1", "lb"),
      usd(300),
    ];

    expect(
      comparablePrices(purchases).map(({ item, perUnit }) => [
        purchases.indexOf(item),
        perUnit,
      ]),
    ).toEqual([
      [0, cad(500)],
      [2, cad(441)],
    ]);
    expect(comparablePrices([])).toEqual([]);
  });
});

describe("sizePrice", () => {
  it.each<
    [
      string,
      Partial<ReturnType<typeof product>>,
      ProductPurchaseOut,
      [number, string] | null,
    ]
  >([
    [
      "a weighed line is priced per its own unit",
      { size_value: null, size_unit: null },
      bought(226, "0.452", "kg"),
      [500, "/kg"],
    ],
    [
      "per pound when weighed in pounds",
      { size_value: null, size_unit: null },
      bought(300, "2", "lb"),
      [150, "/lb"],
    ],
    [
      "a 4 L jug per litre",
      { size_value: "4", size_unit: "l" },
      bought(609, "1", "each"),
      [152, "/L"],
    ],
    [
      "millilitres per litre",
      { size_value: "500", size_unit: "ml" },
      bought(299, "1", null),
      [598, "/L"],
    ],
    [
      "a package under a kilogram per 100 g",
      { size_value: "475", size_unit: "g" },
      bought(1299, "1", "each"),
      [273, "/100 g"],
    ],
    [
      "a kilogram or more per kg",
      { size_value: "1.36", size_unit: "kg" },
      bought(1099, "1", "each"),
      [808, "/kg"],
    ],
    [
      "two packages share the price",
      { size_value: "1", size_unit: "kg" },
      bought(1000, "2", "each"),
      [500, "/kg"],
    ],
    [
      "pounds as kilograms",
      { size_value: "4", size_unit: "lb" },
      bought(1814, "1", "each"),
      [1000, "/kg"],
    ],
    [
      "ounces as 100 g",
      { size_value: "16", size_unit: "oz" },
      bought(454, "1", "each"),
      [100, "/100 g"],
    ],
    [
      "a size in items says nothing",
      { size_value: "24", size_unit: "each" },
      bought(899, "1", "each"),
      null,
    ],
    [
      "no size says nothing",
      { size_value: null, size_unit: null },
      bought(899, "1", null),
      null,
    ],
    [
      "a zero size says nothing",
      { size_value: "0", size_unit: "g" },
      bought(899, "1", null),
      null,
    ],
  ])("%s", (_, size, purchase, expected) => {
    expect(sizePrice(product(size), purchase)).toEqual(
      expected === null
        ? null
        : { price: cad(expected[0]), suffix: expected[1] },
    );
  });
});
