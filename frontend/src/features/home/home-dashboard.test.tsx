import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SUMMARY_POLL_INTERVAL_MS } from "@/features/home/api";
import { productSummary } from "@/test/fixtures/products";
import { cad, receiptSummary } from "@/test/fixtures/receipts";
import { monthSummary, type MonthSummaryOut } from "@/test/fixtures/summary";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const november: MonthSummaryOut = monthSummary({
  spent: [cad(40035)],
  saved: [cad(2600)],
  trips: 5,
  weeks: [
    { start: "2026-11-01", end: "2026-11-07", spent: [cad(33200)] },
    { start: "2026-11-08", end: "2026-11-14", spent: [cad(6835)] },
    { start: "2026-11-15", end: "2026-11-21", spent: [] },
    { start: "2026-11-22", end: "2026-11-30", spent: [] },
  ],
  stores: [
    { store_name: "COSTCO WHOLESALE #1234", spent: cad(20319) },
    { store_name: "REAL CANADIAN SUPERSTORE #1077", spent: cad(15512) },
    { store_name: "METRO", spent: cad(4204) },
  ],
  open_reviews: 2,
  unconfirmed_products: 34,
  recent: [
    receiptSummary({
      id: 3,
      store_name: "METRO",
      purchased_on: "2026-11-14",
      total: cad(4204),
    }),
    receiptSummary({ id: 2, purchased_on: "2026-11-13", total: cad(2618) }),
  ],
});

function serveSummary(...bodies: MonthSummaryOut[]): string[] {
  const months: string[] = [];
  let call = 0;
  server.use(
    http.get("/api/summary", ({ request }) => {
      months.push(new URL(request.url).searchParams.get("month") ?? "");
      const body = bodies[Math.min(call, bodies.length - 1)];
      call += 1;
      return HttpResponse.json(body);
    }),
  );
  return months;
}

async function card(name: string): Promise<HTMLElement> {
  return screen.findByRole("region", { name });
}

describe("Home", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date(2026, 10, 18, 12));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the month, stores, what needs you and recent receipts with the exact copy", async () => {
    const months = serveSummary(november);

    renderApp("/");

    const month = await card("November so far");
    expect(months).toEqual(["2026-11"]);
    expect(textOf(month)).toContain("$400.35");
    expect(textOf(month)).toContain("5 trips · $26.00 saved on deals");
    expect(
      within(month)
        .getAllByRole("listitem")
        .map((week) => textOf(week)),
    ).toEqual(["Nov 1–7$332.00", "Nov 8–14$68.35", "Nov 15–21—", "Nov 22–30"]);
    expect(
      within(month)
        .getAllByTestId("week-bar")
        .map((bar) => bar.style.height),
    ).toEqual(["100%", expect.stringMatching(/^20\.58/), "4%", "4%"]);
    expect(
      within(month).getByRole("link", { name: "All receipts" }),
    ).toHaveAttribute("href", "/receipts");

    expect(
      within(await card("By store"))
        .getAllByRole("listitem")
        .map((store) => textOf(store)),
    ).toEqual([
      "Costco$203.19",
      "Real Canadian Superstore$155.12",
      "Metro$42.04",
    ]);

    const needs = await card("Needs you");
    expect(
      within(needs).getByRole("link", { name: "2 receipts to review" }),
    ).toHaveAttribute("href", "/review");
    expect(
      within(needs).getByRole("link", {
        name: "34 products have guessed details",
      }),
    ).toHaveAttribute("href", "/products?needsALook=true");

    const recent = await card("Recent");
    expect(
      within(recent)
        .getAllByRole("link")
        .map((link) => [textOf(link), link.getAttribute("href")]),
    ).toEqual([
      ["See all", "/receipts"],
      ["MetroSat, Nov 14$42.04", "/receipts/3"],
      ["CostcoFri, Nov 13$26.18", "/receipts/2"],
    ]);
  });

  it("opens Products on the unconfirmed ones from Needs you", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    serveSummary(monthSummary({ ...november, open_reviews: 0 }));
    server.use(
      http.get("/api/products", () => HttpResponse.json([productSummary()])),
    );

    renderApp("/");
    await user.click(
      within(await card("Needs you")).getByRole("link", {
        name: /guessed details/,
      }),
    );

    expect(
      await screen.findByRole("button", { name: /needs a look/i }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("invites a brand-new install to scan its first receipt", async () => {
    renderApp("/");

    expect(
      await screen.findByRole("heading", { name: "Scan your first receipt" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Scan a receipt" }),
    ).toHaveAttribute("href", "/scan");
  });

  it("says all caught up instead of Needs you when nothing waits", async () => {
    serveSummary({ ...november, open_reviews: 0, unconfirmed_products: 0 });

    renderApp("/");

    await card("Recent");
    expect(screen.queryByRole("region", { name: "Needs you" })).toBeNull();
    expect(textOf(await screen.findByRole("main"))).toContain("All caught up");
  });

  it("shows a receipt being read until it is done, then refetches what it changed", async () => {
    serveSummary({ ...november, processing: 1 }, november);
    let reviewFetches = 0;
    server.use(
      http.get("/api/reviews", () => {
        reviewFetches += 1;
        return HttpResponse.json([]);
      }),
    );

    renderApp("/");

    expect(await screen.findByText("Reading your receipt…")).toBeVisible();
    await waitFor(() => {
      expect(reviewFetches).toBe(1);
    });
    await act(() => vi.advanceTimersByTimeAsync(SUMMARY_POLL_INTERVAL_MS));
    await waitFor(() => {
      expect(screen.queryByText("Reading your receipt…")).toBeNull();
    });
    await waitFor(() => {
      expect(reviewFetches).toBe(2);
    });
  });

  it("stops polling once nothing is being read", async () => {
    const months = serveSummary(november);

    renderApp("/");
    await card("Recent");
    await act(() => vi.advanceTimersByTimeAsync(SUMMARY_POLL_INTERVAL_MS * 3));

    expect(months).toHaveLength(1);
  });
});
