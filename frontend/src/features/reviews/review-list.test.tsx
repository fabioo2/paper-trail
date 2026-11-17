import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { cad } from "@/test/fixtures/receipts";
import { dateIssue, review, reviewSummary } from "@/test/fixtures/reviews";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

function serveReviews(...reviews: ReturnType<typeof reviewSummary>[]) {
  server.use(
    http.get("/api/reviews", () => HttpResponse.json(reviews)),
    http.get("/api/reviews/:reviewId", () => HttpResponse.json(review())),
  );
}

describe("Review list", () => {
  it("shows each waiting receipt with its store, date, total and why it waits", async () => {
    serveReviews(
      reviewSummary(),
      reviewSummary({
        id: 8,
        issues: [dateIssue],
        reading: null,
      }),
    );

    renderApp("/review");

    await screen.findByRole("heading", { name: "To review" });
    const main = screen.getByRole("main");
    const rows = within(within(main).getByRole("list")).getAllByRole("link");
    const [costco, unreadable] = rows.map((row) => textOf(row));
    for (const part of [
      "COSTCO WHOLESALE #1234",
      formatDate("2026-11-01"),
      "Totals don't add up",
      "1 issue",
      formatMoney(cad(1654)),
    ])
      expect(costco).toContain(part);
    for (const part of [
      "Unreadable receipt",
      "Date unknown",
      "Check the date",
      "1 issue",
    ])
      expect(unreadable).toContain(part);
    expect(rows).toHaveLength(2);
  });

  it("opens a review from its row", async () => {
    serveReviews(reviewSummary());
    const user = userEvent.setup();
    const { router } = renderApp("/review");

    await user.click(await screen.findByRole("link", { name: /costco/i }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/review/7");
    });
  });

  it("says there is nothing to review when no receipt waits", async () => {
    renderApp("/review");

    expect(
      await screen.findByRole("heading", { name: "Nothing to review" }),
    ).toBeInTheDocument();
  });

  it("shows the count of waiting receipts on the Review tab", async () => {
    serveReviews(reviewSummary(), reviewSummary({ id: 8 }));

    renderApp("/scan");

    const tab = within(
      await screen.findByRole("navigation", { name: "Primary" }),
    ).getByRole("link", { name: "Review" });
    await waitFor(() => {
      expect(textOf(tab)).toBe("2Review");
    });
  });

  it("explains when reviews cannot be loaded", async () => {
    server.use(
      http.get("/api/reviews", () => HttpResponse.json(null, { status: 500 })),
    );

    renderApp("/review");

    expect(
      await screen.findByRole("heading", { name: "Reviews are unavailable" }),
    ).toBeInTheDocument();
  });
});
