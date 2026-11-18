/**
 * Calendar dates (`YYYY-MM-DD`) are formatted in UTC, where they were parsed, so the day never
 * shifts in a time zone west of UTC (a purchase date is a day, not an instant).
 */
function format(
  isoDate: string,
  options: Intl.DateTimeFormatOptions,
  locale?: string,
): string {
  return new Intl.DateTimeFormat(locale, {
    ...options,
    timeZone: "UTC",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}

/** The date in the locale's medium style, e.g. `Nov 1, 2026`. */
export function formatDate(isoDate: string, locale?: string): string {
  return format(isoDate, { dateStyle: "medium" }, locale);
}

/** The date with its weekday, e.g. `Thu, Nov 5, 2026`. */
export function formatFullDate(isoDate: string, locale?: string): string {
  return format(
    isoDate,
    { weekday: "short", month: "short", day: "numeric", year: "numeric" },
    locale,
  );
}

/** The month the date falls in, e.g. `November 2026`. */
export function formatMonth(isoDate: string, locale?: string): string {
  return format(isoDate, { month: "long", year: "numeric" }, locale);
}

/** The month's name alone, e.g. `November`. */
export function formatMonthName(isoDate: string, locale?: string): string {
  return format(isoDate, { month: "long" }, locale);
}

/** The month's short name, e.g. `Nov`. */
export function formatShortMonth(isoDate: string, locale?: string): string {
  return format(isoDate, { month: "short" }, locale);
}

/** The short month and day, e.g. `Nov 5`. */
export function formatMonthDay(isoDate: string, locale?: string): string {
  return format(isoDate, { month: "short", day: "numeric" }, locale);
}

/** The date's short weekday, e.g. `Thu`. */
export function formatWeekday(isoDate: string, locale?: string): string {
  return format(isoDate, { weekday: "short" }, locale);
}
