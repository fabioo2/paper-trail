import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import {
  cad,
  lineItem,
  receiptDetail,
  type ReceiptDetailOut,
} from "@/test/fixtures/receipts";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const LINE_MATH_MESSAGE = "line 2: 1 x 8.99 = 8.99 but line total is 9.89";

function serveDetail(detail: ReceiptDetailOut): URL[] {
  const requests: URL[] = [];
  server.use(
    http.get("/api/receipts/:receiptId", ({ request }) => {
      requests.push(new URL(request.url));
      return HttpResponse.json(detail);
    }),
  );
  return requests;
}

async function page(): Promise<HTMLElement> {
  const main = await screen.findByRole("main");
  await within(main).findByRole("heading", { level: 1, name: /costco/i });
  return main;
}

describe("Receipt detail page", () => {
  it("S1-AC-26: fetches /api/receipts/{id} for /receipts/$receiptId", async () => {
    const requests = serveDetail(receiptDetail());

    renderApp("/receipts/7");
    await page();

    expect(requests.map((url) => url.pathname)).toEqual(["/api/receipts/7"]);
  });

  it("S1-AC-26: header shows store, date, total and status", async () => {
    serveDetail(receiptDetail());

    renderApp("/receipts/7");
    const main = await page();

    const header = textOf(main);
    expect(header).toContain("COSTCO WHOLESALE #1234");
    expect(header).toContain(formatDate("2026-11-01"));
    expect(header).toContain(formatMoney(cad(1155)));
    expect(header).toMatch(/completed/i);
  });

  it("S1-AC-26: renders one table row per line item with qty, unit price and total", async () => {
    serveDetail(receiptDetail());

    renderApp("/receipts/7");
    const main = await page();

    const table = within(main).getByRole("table");
    const rows = within(table).getAllByRole("row");
    const bananas = rows.find((row) => textOf(row).includes("Bananas"));
    const eggs = rows.find((row) =>
      textOf(row).includes("Kirkland Organic Eggs 24 ct"),
    );
    if (!bananas || !eggs) throw new Error("missing line item rows");
    expect(textOf(bananas)).toContain("1.36");
    expect(textOf(bananas)).toContain(formatMoney(cad(150)));
    expect(textOf(bananas)).toContain(formatMoney(cad(204)));
    expect(textOf(eggs)).toContain(formatMoney(cad(899)));
    expect(within(table).getAllByRole("columnheader").map(textOf)).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/description|item/i),
        expect.stringMatching(/qty|quantity/i),
        expect.stringMatching(/unit price/i),
        expect.stringMatching(/total/i),
      ]),
    );
  });

  it("S1-AC-26: a discount line without unit price shows its negative total", async () => {
    serveDetail(
      receiptDetail({
        items: [
          lineItem({
            id: 12,
            kind: "discount",
            description: "Instant savings",
            quantity: "1",
            unit_price: null,
            line_total: cad(-500),
          }),
        ],
      }),
    );

    renderApp("/receipts/7");
    const main = await page();

    const row = within(within(main).getByRole("table"))
      .getAllByRole("row")
      .find((candidate) => textOf(candidate).includes("Instant savings"));
    if (!row) throw new Error("missing discount row");
    expect(textOf(row)).toContain(formatMoney(cad(-500)));
    expect(textOf(row)).not.toMatch(/NaN|undefined|null/);
  });

  it("S1-AC-26: shows the totals block (subtotal, tax, total)", async () => {
    serveDetail(receiptDetail());

    renderApp("/receipts/7");
    const main = await page();

    const text = textOf(main);
    expect(text).toMatch(
      new RegExp(`Subtotal\\s*${escapeRegExp(formatMoney(cad(1103)))}`),
    );
    expect(text).toMatch(
      new RegExp(`Tax\\s*${escapeRegExp(formatMoney(cad(52)))}`),
    );
    expect(text).toMatch(
      new RegExp(`Total\\s*${escapeRegExp(formatMoney(cad(1155)))}`),
    );
  });

  it("S1-AC-26: lists the arithmetic issues when the receipt does not add up", async () => {
    serveDetail(
      receiptDetail({
        status: "needs_review",
        stage: "needs_review",
        arithmetic: {
          ok: false,
          issues: [
            {
              code: "line_math",
              message: LINE_MATH_MESSAGE,
              expected: cad(899),
              actual: cad(989),
              position: 1,
            },
            {
              code: "items_vs_subtotal",
              message: "items add up to 11.03 but the subtotal is 11.93",
              expected: cad(1103),
              actual: cad(1193),
              position: null,
            },
          ],
        },
      }),
    );

    renderApp("/receipts/7");
    const main = await page();

    expect(within(main).getByText(LINE_MATH_MESSAGE)).toBeInTheDocument();
    expect(
      within(main).getByText("items add up to 11.03 but the subtotal is 11.93"),
    ).toBeInTheDocument();
    expect(textOf(main)).toMatch(/needs review/i);
  });

  it("S1-AC-26: lists issues that share a code and position, without key warnings", async () => {
    const consoleError = vi.spyOn(console, "error");
    const mismatch = (message: string) => ({
      code: "currency_mismatch" as const,
      message,
      expected: null,
      actual: null,
      position: null,
    });
    serveDetail(
      receiptDetail({
        status: "needs_review",
        stage: "needs_review",
        arithmetic: {
          ok: false,
          issues: [
            mismatch("the subtotal is in USD, not CAD"),
            mismatch("the total is in USD, not CAD"),
          ],
        },
      }),
    );

    renderApp("/receipts/7");
    const main = await page();

    expect(
      within(main).getByText("the subtotal is in USD, not CAD"),
    ).toBeInTheDocument();
    expect(
      within(main).getByText("the total is in USD, not CAD"),
    ).toBeInTheDocument();
    const keyWarnings = consoleError.mock.calls.filter((args) =>
      args.map(String).join(" ").includes("same key"),
    );
    expect(keyWarnings).toEqual([]);
  });

  it("S1-AC-26: shows no issues callout when the arithmetic is ok", async () => {
    serveDetail(receiptDetail());

    renderApp("/receipts/7");
    const main = await page();

    expect(within(main).queryByText(LINE_MATH_MESSAGE)).toBeNull();
    expect(textOf(main)).not.toMatch(
      /does not add up|doesn['’]t add up|issues? found/i,
    );
  });

  it("S1-AC-26: an unknown receipt shows a not-found message, not a crash", async () => {
    const notFound: ProblemDetail = {
      type: "/problems/not-found",
      title: "Not Found",
      status: 404,
      detail: "receipt 999 does not exist",
      instance: "/api/receipts/999",
      errors: null,
    };
    server.use(
      http.get("/api/receipts/:receiptId", () =>
        HttpResponse.json(notFound, {
          status: 404,
          headers: { "content-type": "application/problem+json" },
        }),
      ),
    );

    renderApp("/receipts/999");

    const main = await screen.findByRole("main");
    expect(
      await within(main).findByRole("heading", { name: /not found/i }),
    ).toBeInTheDocument();
  });
});

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
