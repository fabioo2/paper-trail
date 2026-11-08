/**
 * A calendar date (`YYYY-MM-DD`) in the locale's medium style, e.g. `Nov 1, 2026`.
 * The date is formatted in UTC, where it was parsed, so the day never shifts in a time zone
 * west of UTC (a purchase date is a day, not an instant).
 */
export function formatDate(isoDate: string, locale?: string): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}
