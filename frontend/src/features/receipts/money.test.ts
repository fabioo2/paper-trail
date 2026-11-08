import { describe, expect, it } from "vitest";

import { formatMoney } from "@/features/receipts/money";

describe("formatMoney", () => {
  it("S1-AC-27: renders negative Canadian dollars in en-CA as -$2.50", () => {
    expect(formatMoney({ amount_cents: -250, currency: "CAD" }, "en-CA")).toBe(
      "-$2.50",
    );
  });

  it.each([
    [0, "$0.00"],
    [1, "$0.01"],
    [123456, "$1,234.56"],
    [100000000, "$1,000,000.00"],
  ])("S1-AC-27: %i cents is %s", (cents, expected) => {
    expect(formatMoney({ amount_cents: cents, currency: "CAD" }, "en-CA")).toBe(
      expected,
    );
  });

  it("S1-AC-27: uses the money's own currency", () => {
    expect(formatMoney({ amount_cents: 250, currency: "USD" }, "en-CA")).toBe(
      "US$2.50",
    );
    expect(formatMoney({ amount_cents: 250, currency: "EUR" }, "en-CA")).toBe(
      "€2.50",
    );
  });

  it("S1-AC-27: follows the locale's conventions", () => {
    expect(
      formatMoney({ amount_cents: 123456, currency: "CAD" }, "fr-CA"),
    ).toBe(
      new Intl.NumberFormat("fr-CA", {
        style: "currency",
        currency: "CAD",
      }).format(1234.56),
    );
  });

  it("S1-AC-27: never goes through binary floating point rounding", () => {
    // 0.1 + 0.2 style errors would show up as 1.0000000000000002 etc.
    expect(formatMoney({ amount_cents: 110, currency: "CAD" }, "en-CA")).toBe(
      "$1.10",
    );
    expect(formatMoney({ amount_cents: 1005, currency: "CAD" }, "en-CA")).toBe(
      "$10.05",
    );
  });
});
