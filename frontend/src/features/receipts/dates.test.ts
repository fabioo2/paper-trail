import { describe, expect, it, vi } from "vitest";

import { formatDate } from "@/features/receipts/dates";

/** What a calendar date looks like in `locale`, independent of the machine's time zone. */
function expected(isoDate: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}

describe("formatDate", () => {
  it("S1-AC-26: formats an ISO calendar date with the medium date style", () => {
    expect(formatDate("2026-11-01", "en-CA")).toBe(
      expected("2026-11-01", "en-CA"),
    );
    expect(formatDate("2026-11-01", "en-CA")).toMatch(/Nov\.? 1, 2026/);
  });

  it("S1-AC-26: never shifts the day, whatever the time zone (a date is not an instant)", () => {
    // West of UTC, `new Date("2026-01-01")` is still Dec 31 locally: the classic off-by-one.
    vi.stubEnv("TZ", "America/Toronto");

    for (const day of ["2026-01-01", "2026-03-08", "2026-12-31"]) {
      expect(formatDate(day, "en-CA")).toBe(expected(day, "en-CA"));
    }
  });

  it("S1-AC-26: follows the locale", () => {
    expect(formatDate("2026-11-01", "fr-CA")).toBe(
      expected("2026-11-01", "fr-CA"),
    );
  });
});
