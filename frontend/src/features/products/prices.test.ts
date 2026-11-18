import { describe, expect, it } from "vitest";

import { unitPriceSummary } from "@/features/products/prices";
import {
  productPurchase,
  type ProductPurchaseOut,
} from "@/test/fixtures/products";
import { cad } from "@/test/fixtures/receipts";

function bought(
  paid: number,
  quantity: string,
  unit: ProductPurchaseOut["unit"],
): ProductPurchaseOut {
  return productPurchase({ paid: cad(paid), quantity, unit });
}

describe("unitPriceSummary", () => {
  it.each<{
    name: string;
    purchases: ProductPurchaseOut[];
    expected: [number, number, ProductPurchaseOut["unit"], number];
  }>([
    {
      name: "a newest purchase alone is both lowest and average",
      purchases: [bought(441, "1", "kg")],
      expected: [441, 441, "kg", 1],
    },
    {
      name: "another currency is left out",
      purchases: [
        bought(899, "1", "each"),
        productPurchase({ paid: { amount_cents: 100, currency: "USD" } }),
      ],
      expected: [899, 899, "each", 1],
    },
    {
      name: "each and kg are not compared",
      purchases: [bought(899, "1", "each"), bought(100, "1", "kg")],
      expected: [899, 899, "each", 1],
    },
    {
      name: "an unknown unit counts as each",
      purchases: [bought(899, "1", "each"), bought(1299, "1", null)],
      expected: [899, 1099, "each", 2],
    },
    {
      name: "pounds are compared as kilograms: $2.00/lb is $4.41/kg",
      purchases: [bought(500, "1", "kg"), bought(200, "1", "lb")],
      expected: [441, 470, "kg", 2],
    },
    {
      name: "kilograms are compared as pounds when the newest is in pounds",
      purchases: [bought(200, "1", "lb"), bought(441, "1", "kg")],
      expected: [200, 200, "lb", 2],
    },
    {
      name: "a converted price that rounds to a kg price is the same price",
      purchases: [bought(441, "1", "kg"), bought(200, "1", "lb")],
      expected: [441, 441, "kg", 2],
    },
    {
      name: "a converted price of exactly half a cent rounds up: 0.90718474 kg for $2.39 is $1.195/lb",
      purchases: [bought(300, "1", "lb"), bought(239, "0.90718474", "kg")],
      expected: [120, 210, "lb", 2],
    },
    {
      name: "full precision is kept until the final rounding",
      purchases: [bought(1001, "10", "each"), bought(503, "5", "each")],
      expected: [100, 100, "each", 2],
    },
  ])("$name", ({ purchases, expected: [lowest, average, unit, count] }) => {
    expect(unitPriceSummary(purchases)).toEqual({
      lowest: cad(lowest),
      average: cad(average),
      unit,
      count,
    });
  });

  it("is null without purchases", () => {
    expect(unitPriceSummary([])).toBeNull();
  });
});
