import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { FailureReason } from "@/features/receipts/api";
import { formatFullDate } from "@/features/receipts/dates";
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

const FAILURE_COPY: [FailureReason, string][] = [
  [
    "budget_reached",
    "This month's Claude budget is used up. Raise the budget or wait until next month, then try again.",
  ],
  [
    "claude_unavailable",
    "Claude couldn't read it just now. Try again in a minute.",
  ],
  [
    "no_api_key",
    "No Claude API key is set on the server. Add ANTHROPIC_API_KEY to .env, then try again.",
  ],
  [
    "unreadable_photo",
    "The photo couldn't be read. Retake it with the whole receipt flat, in focus and well lit.",
  ],
  ["stopped_unexpectedly", "Paper Trail stopped while reading it. Try again."],
  ["unknown", "Something went wrong while reading it. Try again."],
];

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

  it("S1-AC-26: header shows the tidy store name, full date, line count and total, and no badge once completed", async () => {
    serveDetail(receiptDetail());

    renderApp("/receipts/7");
    const main = await page();

    expect(
      within(main).getByRole("link", { name: "Receipts" }),
    ).toHaveAttribute("href", "/receipts");
    expect(within(main).getByRole("heading", { level: 1 })).toHaveTextContent(
      /^Costco$/,
    );
    const text = textOf(main);
    expect(text).toContain(`${formatFullDate("2026-11-01")} · 2 lines`);
    expect(text).toContain(`Total${formatMoney(cad(1155))}`);
    expect(text).not.toMatch(/completed|you saved/i);
  });

  it("reads each line as a stacked row: name, printed text or category, and what was paid", async () => {
    serveDetail(
      receiptDetail({
        items: [
          lineItem(),
          lineItem({
            id: 11,
            position: 1,
            raw_text: "1100231 KS EGGS 24CT",
            description: "Kirkland Organic Eggs 24 ct",
          }),
          lineItem({
            id: 12,
            position: 2,
            raw_text: "1281 BANANAS",
            description: "Bananas",
            quantity: "1.36",
            unit_price: cad(150),
            line_total: cad(204),
            category: "produce",
          }),
          lineItem({
            id: 13,
            position: 3,
            kind: "discount",
            raw_text: "TPD/1362394",
            description: "Instant savings",
            unit_price: null,
            line_total: cad(-500),
            category: null,
          }),
        ],
      }),
    );

    renderApp("/receipts/7");
    const main = await page();
    await within(main).findByText("Produce");

    const rows = within(within(main).getByRole("list", { name: "Items" }))
      .getAllByRole("listitem")
      .map(textOf);
    expect(rows).toEqual([
      `Kirkland Organic Eggs 24 ctKS ORG EGGS 24CT${formatMoney(cad(899))}`,
      `Kirkland Organic Eggs 24 ctKS EGGS 24CT${formatMoney(cad(899))}`,
      `Bananas ×1.36Produce${formatMoney(cad(204))}`,
      `Instant savingsTPD/1362394${formatMoney(cad(-500))}`,
    ]);
    expect(within(main).queryByRole("table")).toBeNull();
    expect(
      main.querySelector('[data-category="produce"]')?.getAttribute("style"),
    ).toBe("background-color: var(--cat-produce);");
  });

  it("folds discounts into their item, collapses repeats to ×N and links matched rows to the product", async () => {
    const choc = {
      raw_text: "1362394 CHOC PISTACH",
      description: "Choc Pistach",
      product_id: 42,
      product_name: "Chocolate Pistachio",
      product_category: "snacks_candy",
    } as const;
    serveDetail(
      receiptDetail({
        savings: cad(800),
        items: [0, 2].flatMap((position) => [
          lineItem({
            ...choc,
            id: 20 + position,
            position,
            line_total: cad(1699),
          }),
          lineItem({
            id: 21 + position,
            position: position + 1,
            kind: "discount",
            raw_text: "TPD/1362394",
            description: "Instant savings",
            unit_price: null,
            line_total: cad(-400),
            applies_to_position: position,
          }),
        ]),
      }),
    );

    renderApp("/receipts/7");
    const main = await page();

    const [row, ...others] = within(
      within(main).getByRole("list", { name: "Items" }),
    ).getAllByRole("listitem");
    if (!row) throw new Error("expected one row");
    expect(others).toEqual([]);
    expect(textOf(row)).toBe(
      `Chocolate Pistachio ×2CHOC PISTACH${formatMoney(cad(2598))}saved ${formatMoney(cad(800))}`,
    );
    expect(within(row).getByRole("link")).toHaveAttribute(
      "href",
      "/products/42",
    );
    expect(textOf(main)).toContain(`You saved${formatMoney(cad(800))}`);
    expect(textOf(main)).toContain("· 4 lines");
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
    expect(textOf(main)).toMatch(/needs a look/i);
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

  it("shows neither a failure nor Try again for a receipt that did not fail", async () => {
    serveDetail(receiptDetail({ status: "processing", stage: "reading" }));

    renderApp("/receipts/7");
    const main = await page();

    expect(
      within(main).queryByRole("button", { name: "Try again" }),
    ).toBeNull();
    expect(textOf(main)).not.toMatch(/couldn['’]t read/i);
  });

  it.each(FAILURE_COPY)(
    "a %s failure says why, with Try again",
    async (reason, message) => {
      serveDetail(
        receiptDetail({
          status: "failed",
          stage: "failed",
          failure_reason: reason,
        }),
      );

      renderApp("/receipts/7");
      const main = await page();

      const failure = within(main)
        .getByRole("heading", { level: 2, name: /couldn['’]t read/i })
        .closest("section");
      if (!failure) throw new Error("expected the failure section");
      expect(textOf(failure)).toBe(
        `Couldn't read this receipt${message}Try again`,
      );
    },
  );

  it("Try again tapped twice retries once and shows the receipt processing", async () => {
    const requests = serveDetail(
      receiptDetail({
        status: "failed",
        stage: "failed",
        failure_reason: "unknown",
      }),
    );
    const retries: string[] = [];
    server.use(
      http.post("/api/receipts/:receiptId/retry", ({ request }) => {
        retries.push(new URL(request.url).pathname);
        return HttpResponse.json(
          { id: 7, status: "processing", stage: "queued" },
          { status: 202 },
        );
      }),
    );
    const user = userEvent.setup();
    renderApp("/receipts/7");
    const main = await page();
    serveDetail(receiptDetail({ status: "processing", stage: "queued" }));

    await user.dblClick(
      within(main).getByRole("button", { name: "Try again" }),
    );

    await waitFor(() => {
      expect(
        within(main).queryByRole("button", { name: "Try again" }),
      ).toBeNull();
    });
    expect(textOf(main)).toMatch(/reading/i);
    expect(retries).toEqual(["/api/receipts/7/retry"]);
    expect(requests).toHaveLength(1);
  });

  it.each<{ name: string; respond: () => Response; toast: RegExp }>([
    {
      name: "a 409",
      respond: () => retryProblem(409, "Conflict"),
      toast: /already being read again/i,
    },
    {
      name: "a 500",
      respond: () => retryProblem(500, "Internal Server Error"),
      toast: /went wrong on the server/i,
    },
    {
      name: "a network failure",
      respond: () => HttpResponse.error(),
      toast: /connection/i,
    },
  ])(
    "$name on Try again is explained and the button works again",
    async ({ respond, toast }) => {
      serveDetail(
        receiptDetail({
          status: "failed",
          stage: "failed",
          failure_reason: "unknown",
        }),
      );
      let retries = 0;
      server.use(
        http.post("/api/receipts/:receiptId/retry", () => {
          retries += 1;
          return respond();
        }),
      );
      const user = userEvent.setup();
      renderApp("/receipts/7");
      const main = await page();
      const refetched = serveDetail(
        receiptDetail({
          status: "failed",
          stage: "failed",
          failure_reason: "unknown",
        }),
      );

      await user.click(within(main).getByRole("button", { name: "Try again" }));

      expect(await screen.findByText("Couldn't try again")).toBeInTheDocument();
      expect(screen.getByText(toast)).toBeInTheDocument();
      await waitFor(() => {
        expect(refetched.length).toBeGreaterThan(0);
      });
      const button = within(main).getByRole("button", { name: "Try again" });
      await waitFor(() => {
        expect(button).toBeEnabled();
      });
      await user.click(button);
      await waitFor(() => {
        expect(retries).toBe(2);
      });
    },
  );
});

function retryProblem(status: number, title: string): Response {
  const body: ProblemDetail = {
    type: "about:blank",
    title,
    status,
    detail: null,
    instance: "/api/receipts/7/retry",
    errors: null,
  };
  return HttpResponse.json(body, {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
