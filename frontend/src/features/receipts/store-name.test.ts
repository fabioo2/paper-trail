import { describe, expect, it } from "vitest";

import { displayStoreName } from "@/features/receipts/store-name";

describe("displayStoreName", () => {
  it.each([
    ["COSTCO WHOLESALE", "Costco"],
    ["COSTCO WHOLESALE #1234", "Costco"],
    ["metro", "Metro"],
    ["Real Canadian SuperStore", "Real Canadian Superstore"],
    ["WALMART SUPERCENTRE 3150", "Walmart"],
    ["Farm Boy #12", "Farm Boy"],
    ["SHOPPERS DRUG MART STORE 1234", "Shoppers Drug Mart"],
    ["LOBLAW'S INC.", "Loblaw's"],
    ["SAINT-HUBERT", "Saint-Hubert"],
    ["iga", "IGA"],
    ["Costco Wholesale Windsor #534", "Costco"],
    ["FRESHCO WINDSOR", "FreshCo"],
    ["Metropolitan Market", "Metropolitan Market"],
    ["  Farm   Boy  ", "Farm Boy"],
    ["Wholesale", "Wholesale"],
    ["7-ELEVEN", "7-Eleven"],
    ["", ""],
  ])("%j reads as %j", (stored, shown) => {
    expect(displayStoreName(stored)).toBe(shown);
  });
});
