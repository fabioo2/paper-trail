import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { formatWeekday } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import {
  cad,
  receiptPage,
  receiptSummary,
  type ReceiptPageOut,
} from "@/test/fixtures/receipts";
import { intersectAll, showEverything } from "@/test/intersection-observer";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const SKELETON = '[data-slot="skeleton"]';

const firstPage = receiptPage(
  [
    receiptSummary({ id: 3, store_name: "Farm Boy", total: cad(4520) }),
    receiptSummary({
      id: 2,
      store_name: "WALMART SUPERCENTRE 3150",
      purchased_on: "2026-10-28",
      total: cad(1099),
      status: "needs_review",
    }),
  ],
  "cursor-after-2",
);
const secondPage = receiptPage([
  receiptSummary({
    id: 1,
    store_name: "COSTCO WHOLESALE #1234",
    purchased_on: "2026-10-20",
  }),
]);

const serverError: ProblemDetail = {
  type: "/problems/internal",
  title: "Internal Server Error",
  status: 500,
  detail: "An unexpected error occurred.",
  instance: "/api/receipts",
  errors: null,
};

/** Serve `pages` keyed by the cursor they answer; record every request URL. */
function servePages(pages: Record<string, ReceiptPageOut>): URL[] {
  const requests: URL[] = [];
  server.use(
    http.get("/api/receipts", ({ request }) => {
      const url = new URL(request.url);
      requests.push(url);
      const page = pages[url.searchParams.get("cursor") ?? ""];
      return page
        ? HttpResponse.json(page)
        : HttpResponse.json(serverError, { status: 500 });
    }),
  );
  return requests;
}

async function receiptLinks(): Promise<HTMLElement[]> {
  const main = await screen.findByRole("main");
  return within(main).findAllByRole("link", {
    name: /farm boy|walmart|costco/i,
  });
}

function monthHeadings(): string[] {
  return screen.getAllByRole("heading", { level: 2 }).map(textOf);
}

describe("Receipts tab", () => {
  it("S1-AC-26: shows skeleton rows while the first page loads", async () => {
    server.use(
      http.get("/api/receipts", async () => {
        await delay("infinite");
        return HttpResponse.json(firstPage);
      }),
    );

    renderApp("/receipts");

    const main = await screen.findByRole("main");
    await waitFor(() => {
      expect(main.querySelectorAll(SKELETON).length).toBeGreaterThan(0);
    });
    expect(within(main).queryByRole("link", { name: /farm boy/i })).toBeNull();
  });

  it("S1-AC-26: shows the empty state when there are no receipts", async () => {
    renderApp("/receipts");

    const main = await screen.findByRole("main");
    expect(
      await within(main).findByRole("heading", { name: /no receipts yet/i }),
    ).toBeInTheDocument();
    expect(main.querySelectorAll(SKELETON)).toHaveLength(0);
  });

  it("S1-AC-26: lists receipts with day, weekday, tidy store, item count, savings, total and a badge only when not completed", async () => {
    servePages({
      "": receiptPage([
        receiptSummary({
          id: 3,
          store_name: "FARM BOY #12",
          total: cad(4520),
          item_count: 12,
          savings: cad(2600),
        }),
        receiptSummary({
          id: 2,
          store_name: "WALMART SUPERCENTRE 3150",
          purchased_on: "2026-10-28",
          total: cad(1099),
          item_count: 1,
          status: "needs_review",
        }),
      ]),
    });

    renderApp("/receipts");

    const [farmBoy, walmart] = await receiptLinks();
    if (!farmBoy || !walmart) throw new Error("expected two receipt rows");
    expect(farmBoy).toHaveAttribute("href", "/receipts/3");
    expect(walmart).toHaveAttribute("href", "/receipts/2");
    expect(textOf(farmBoy)).toBe(
      `1${formatWeekday("2026-11-01")}Farm Boy12 items · saved ${formatMoney(cad(2600))}${formatMoney(cad(4520))}`,
    );
    expect(textOf(walmart)).toBe(
      `28${formatWeekday("2026-10-28")}Walmart1 item${formatMoney(cad(1099))}Needs a look`,
    );
  });

  it("groups the receipts under one heading per month with its total and trip count, undated last", async () => {
    servePages({
      "": receiptPage([
        receiptSummary({ id: 4, purchased_on: "2026-11-14", total: cad(4204) }),
        receiptSummary({ id: 3, purchased_on: "2026-11-05", total: null }),
        receiptSummary({
          id: 2,
          purchased_on: "2026-09-24",
          total: cad(28944),
        }),
        receiptSummary({ id: 1, purchased_on: null, total: cad(100) }),
      ]),
    });

    renderApp("/receipts");
    await receiptLinks();

    expect(monthHeadings()).toEqual([
      `November 2026${formatMoney(cad(4204))} · 2 trips`,
      `September 2026${formatMoney(cad(28944))} · 1 trip`,
      `No date${formatMoney(cad(100))} · 1 trip`,
    ]);
  });

  it("a month with receipts in two currencies shows a total for each", async () => {
    const usd = (cents: number) => ({ amount_cents: cents, currency: "USD" });
    servePages({
      "": receiptPage([
        receiptSummary({ id: 4, purchased_on: "2026-11-14", total: cad(4000) }),
        receiptSummary({ id: 3, purchased_on: "2026-11-10", total: usd(2000) }),
        receiptSummary({ id: 2, purchased_on: "2026-11-05", total: cad(35) }),
        receiptSummary({ id: 1, purchased_on: "2026-11-02", total: usd(500) }),
      ]),
    });

    renderApp("/receipts");
    await receiptLinks();

    expect(monthHeadings()).toEqual([
      `November 2026${formatMoney(cad(4035))} + ${formatMoney(usd(2500))} · 4 trips`,
    ]);
  });

  it("a month split across two pages keeps one heading, and its total grows as the next page loads", async () => {
    servePages({
      "": receiptPage(
        [
          receiptSummary({
            id: 3,
            purchased_on: "2026-11-14",
            total: cad(1000),
          }),
          receiptSummary({
            id: 2,
            purchased_on: "2026-11-02",
            total: cad(500),
          }),
        ],
        "cursor-after-2",
      ),
      "cursor-after-2": receiptPage([
        receiptSummary({ id: 1, purchased_on: "2026-11-01", total: cad(250) }),
        receiptSummary({ id: 0, purchased_on: "2026-10-31", total: cad(99) }),
      ]),
    });

    renderApp("/receipts");
    await receiptLinks();
    expect(monthHeadings()).toEqual([
      `November 2026${formatMoney(cad(1500))} · 2 trips`,
    ]);

    intersectAll();

    await waitFor(async () => {
      expect(await receiptLinks()).toHaveLength(4);
    });
    expect(monthHeadings()).toEqual([
      `November 2026${formatMoney(cad(1750))} · 3 trips`,
      `October 2026${formatMoney(cad(99))} · 1 trip`,
    ]);
  });

  it("S1-AC-26: a receipt without a date or total still gets a row", async () => {
    servePages({
      "": receiptPage([
        receiptSummary({
          id: 9,
          store_name: "Farm Boy",
          purchased_on: null,
          total: null,
          status: "processing",
        }),
      ]),
    });

    renderApp("/receipts");

    const [row] = await receiptLinks();
    if (!row) throw new Error("expected one receipt row");
    expect(textOf(row)).toMatch(/reading/i);
    expect(textOf(row)).not.toMatch(/NaN|undefined|null/);
  });

  it("S1-AC-26: loads the next page when the end of the list scrolls into view", async () => {
    const requests = servePages({
      "": firstPage,
      "cursor-after-2": secondPage,
    });

    renderApp("/receipts");
    expect(await receiptLinks()).toHaveLength(2);

    intersectAll();

    await waitFor(async () => {
      expect(await receiptLinks()).toHaveLength(3);
    });
    expect(requests.map((url) => url.searchParams.get("cursor"))).toEqual([
      null,
      "cursor-after-2",
    ]);
    const links = await receiptLinks();
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/receipts/3",
      "/receipts/2",
      "/receipts/1",
    ]);

    intersectAll();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requests).toHaveLength(2);
  });

  it("S1-AC-26: shows an error toast with a retry action when loading fails", async () => {
    let fail = true;
    server.use(
      http.get("/api/receipts", () =>
        fail
          ? HttpResponse.json(serverError, { status: 500 })
          : HttpResponse.json(firstPage),
      ),
    );
    const user = userEvent.setup();

    renderApp("/receipts");

    expect(
      await screen.findByText(/could(n['’]t| not) load receipts/i),
    ).toBeInTheDocument();
    const retry = await screen.findByRole("button", { name: /retry/i });
    fail = false;
    await user.click(retry);

    expect(await receiptLinks()).toHaveLength(2);
  });

  it("S1-AC-26: keeps loading pages while the end of the list stays in view", async () => {
    const requests = servePages({
      "": firstPage,
      "cursor-after-2": receiptPage(
        [receiptSummary({ id: 1, store_name: "COSTCO WHOLESALE #1234" })],
        "cursor-after-1",
      ),
      "cursor-after-1": receiptPage([
        receiptSummary({ id: 0, store_name: "Farm Boy Rideau" }),
      ]),
    });
    showEverything(); // a viewport taller than the list: the sentinel never leaves it

    renderApp("/receipts");

    await waitFor(async () => {
      expect(await receiptLinks()).toHaveLength(4);
    });
    expect(requests.map((url) => url.searchParams.get("cursor"))).toEqual([
      null,
      "cursor-after-2",
      "cursor-after-1",
    ]);
  });

  it("S1-AC-26: a failed next page keeps the loaded rows and retries only that page", async () => {
    let failNextPage = true;
    const requests: (string | null)[] = [];
    server.use(
      http.get("/api/receipts", ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        requests.push(cursor);
        if (cursor === null) return HttpResponse.json(firstPage);
        return failNextPage
          ? HttpResponse.json(serverError, { status: 500 })
          : HttpResponse.json(secondPage);
      }),
    );
    const user = userEvent.setup();

    renderApp("/receipts");
    expect(await receiptLinks()).toHaveLength(2);
    intersectAll();

    const retry = await screen.findByRole("button", { name: /retry/i });
    expect(await receiptLinks()).toHaveLength(2);
    expect(screen.queryByText(/receipts are unavailable/i)).toBeNull();

    failNextPage = false;
    await user.click(retry);

    await waitFor(async () => {
      expect(await receiptLinks()).toHaveLength(3);
    });
    expect(requests.at(-1)).toBe("cursor-after-2");
    expect(requests.filter((cursor) => cursor === null)).toHaveLength(1);
  });

  it("S1-AC-26: requests the first page from the same-origin API", async () => {
    const requests = servePages({ "": firstPage });

    renderApp("/receipts");
    await receiptLinks();

    expect(requests[0]?.origin).toBe(window.location.origin);
    expect(requests[0]?.pathname).toBe("/api/receipts");
  });
});
