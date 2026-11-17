import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { ProblemDetail } from "@/test/msw/handlers";
import {
  dateIssue,
  review,
  reviewDraft,
  reviewSummary,
  type ReviewOut,
} from "@/test/fixtures/reviews";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";

const PROBLEM_HEADERS = { "content-type": "application/problem+json" };

const inconsistent: ProblemDetail = {
  type: "/problems/inconsistent-receipt",
  title: "Unprocessable Entity",
  status: 422,
  detail:
    "the receipt does not add up: items add up to 15.57 but the subtotal is 15.75",
  instance: "/api/reviews/7/resolve",
  errors: [
    {
      loc: ["body"],
      msg: "items add up to 15.57 but the subtotal is 15.75",
      type: "items_vs_subtotal",
    },
  ],
};

interface Requests {
  list: number;
  answers: unknown[];
}

/** Serve the review and its list row; record every list read and every answer. */
function serveReview(stored: ReviewOut = review()): Requests {
  const requests: Requests = { list: 0, answers: [] };
  server.use(
    http.get("/api/reviews", () => {
      requests.list += 1;
      return HttpResponse.json([reviewSummary()]);
    }),
    http.get("/api/reviews/:reviewId", () => HttpResponse.json(stored)),
    http.post("/api/reviews/:reviewId/resolve", async ({ request }) => {
      requests.answers.push(await request.json());
      return HttpResponse.json(
        { receipt_id: 3, status: "processing", stage: "queued" },
        { status: 202 },
      );
    }),
  );
  return requests;
}

async function form(): Promise<HTMLElement> {
  const main = await screen.findByRole("main");
  await within(main).findByRole("heading", { level: 1 });
  return main;
}

function item(number: number): HTMLElement {
  return screen.getByRole("group", { name: `Item ${String(number)}` });
}

describe("Review editor", () => {
  it("shows the photo, the issues and the reading with discounts folded into their items", async () => {
    serveReview();

    renderApp("/review/7");
    const main = await form();

    expect(
      within(main).getByRole("heading", {
        level: 1,
        name: "COSTCO WHOLESALE #1234",
      }),
    ).toBeInTheDocument();
    expect(
      within(main).getByText("items add up to 15.57 but the subtotal is 15.75"),
    ).toBeInTheDocument();
    expect(
      within(main).getByRole("link", { name: /the receipt/i }),
    ).toHaveAttribute(
      "href",
      new URL("/api/receipts/3/image", window.location.origin).toString(),
    );
    expect(screen.getByLabelText("Purchase date")).toHaveValue("2026-11-01");
    expect(screen.getByLabelText("Store")).toHaveValue(
      "COSTCO WHOLESALE #1234",
    );
    expect(
      [1, 2].map((number) =>
        ["Description", "Qty", "Paid", "Discount"].map(
          (label) =>
            within(item(number)).getByLabelText<HTMLInputElement>(label).value,
        ),
      ),
    ).toEqual([
      ["Kirkland Organic Eggs", "1", "8.99", "1.00"],
      ["Kirkland 2% Milk 4 L", "2", "6.58", ""],
    ]);
    expect(
      ["Subtotal", "Tax", "Total"].map(
        (label) => screen.getByLabelText<HTMLInputElement>(label).value,
      ),
    ).toEqual(["15.75", "0.79", "16.54"]);
  });

  it("puts the date first and focuses it when the date is the problem", async () => {
    serveReview(
      review({
        issues: [dateIssue],
        draft: reviewDraft({ purchased_on: null }),
      }),
    );

    renderApp("/review/7");
    await form();

    const date = screen.getByLabelText("Purchase date");
    await waitFor(() => {
      expect(date).toHaveFocus();
    });
    expect(date).toHaveValue("");
    expect(
      within(
        screen.getByRole("region", { name: "Check the date" }),
      ).getByLabelText("Purchase date"),
    ).toBe(date);
  });

  it("saves the corrected reading, then returns to the refreshed list", async () => {
    const requests = serveReview();
    const user = userEvent.setup();
    const { router } = renderApp("/review");
    await user.click(await screen.findByRole("link", { name: /costco/i }));
    await form();

    await user.clear(within(item(1)).getByLabelText("Discount"));
    await user.clear(within(item(1)).getByLabelText("Paid"));
    await user.type(within(item(1)).getByLabelText("Paid"), "9.17");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Receipt saved")).toBeInTheDocument();
    const draft = reviewDraft();
    expect(requests.answers).toEqual([
      {
        accept: false,
        force: false,
        draft: {
          ...draft,
          lines: [
            {
              ...draft.lines[0],
              unit_price_cents: null,
              line_total_cents: 917,
            },
            draft.lines[2],
          ],
        },
      },
    ]);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/review");
    });
    await waitFor(() => {
      expect(requests.list).toBe(2);
    });
  });

  it("Accept as read accepts the reading, and is off once something changed", async () => {
    const requests = serveReview();
    const user = userEvent.setup();
    renderApp("/review/7");
    await form();

    await user.type(screen.getByLabelText("Store"), "!");
    expect(
      screen.getByRole("button", { name: "Accept as read" }),
    ).toBeDisabled();
    await user.type(screen.getByLabelText("Store"), "{Backspace}");
    await user.click(screen.getByRole("button", { name: "Accept as read" }));

    expect(await screen.findByText("Reading accepted")).toBeInTheDocument();
    expect(requests.answers).toEqual([{ accept: true, force: false }]);
  });

  it("offers Save anyway with a reason when the reading still does not add up", async () => {
    const requests = serveReview();
    server.use(
      http.post(
        "/api/reviews/:reviewId/resolve",
        async ({ request }) => {
          requests.answers.push(await request.json());
          return HttpResponse.json(inconsistent, {
            status: 422,
            headers: PROBLEM_HEADERS,
          });
        },
        { once: true },
      ),
    );
    const user = userEvent.setup();
    const { router } = renderApp("/review/7");
    await form();

    await user.click(screen.getByRole("button", { name: "Accept as read" }));

    const refused = await screen.findByRole("alert");
    expect(
      within(refused).getByText(
        "items add up to 15.57 but the subtotal is 15.75",
      ),
    ).toBeInTheDocument();
    const anyway = within(refused).getByRole("button", { name: "Save anyway" });
    expect(anyway).toBeDisabled();
    await user.type(
      within(refused).getByLabelText("Why keep it anyway?"),
      " torn ",
    );
    await user.click(anyway);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/review");
    });
    expect(requests.answers).toEqual([
      { accept: true, force: false },
      { accept: true, force: true, reason: "torn" },
    ]);
  });

  it("disables the buttons while saving and shows the API's message on failure", async () => {
    serveReview();
    let release: () => void = () => undefined;
    server.use(
      http.post("/api/reviews/:reviewId/resolve", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(
          {
            ...inconsistent,
            type: "/problems/conflict",
            title: "Conflict",
            status: 409,
            detail: "review 7 was already answered: its receipt is being saved",
            errors: null,
          },
          { status: 409, headers: PROBLEM_HEADERS },
        );
      }),
    );
    const user = userEvent.setup();
    renderApp("/review/7");
    await form();

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    });
    expect(
      screen.getByRole("button", { name: "Accept as read" }),
    ).toBeDisabled();
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "review 7 was already answered: its receipt is being saved",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("names the fields to fix and sends nothing", async () => {
    const requests = serveReview();
    const user = userEvent.setup();
    renderApp("/review/7");
    await form();

    await user.clear(screen.getByLabelText("Total"));
    await user.type(screen.getByLabelText("Total"), "16.5.4");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByLabelText("Total")).toHaveAccessibleDescription(
      "Use an amount like 8.99",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Some fields need fixing",
    );
    expect(requests.answers).toEqual([]);
  });

  it("lets a person type in a receipt that could not be read", async () => {
    const requests = serveReview(
      review({ reason: "extraction_failed", issues: [], draft: null }),
    );
    const user = userEvent.setup();
    const { router } = renderApp("/review/7");
    await form();

    expect(screen.queryByRole("button", { name: "Accept as read" })).toBeNull();
    await user.type(screen.getByLabelText("Store"), "Farm Boy");
    await user.type(within(item(1)).getByLabelText("Description"), "Kiwi");
    await user.type(within(item(1)).getByLabelText("Paid"), "2");
    await user.click(screen.getByRole("button", { name: "Add item" }));
    await user.type(within(item(2)).getByLabelText("Description"), "Bread");
    await user.type(within(item(2)).getByLabelText("Paid"), "3.50");
    await user.click(screen.getByRole("button", { name: "Remove item 1" }));
    await user.type(screen.getByLabelText("Total"), "3.50");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/review");
    });
    expect(requests.answers).toEqual([
      {
        accept: false,
        force: false,
        draft: {
          store_name: "Farm Boy",
          purchased_on: null,
          currency: "CAD",
          subtotal_cents: null,
          tax_cents: null,
          total_cents: 350,
          payment_last4: null,
          lines: [
            {
              kind: "item",
              raw_text: "Bread",
              description: "Bread",
              quantity: "1",
              unit_price_cents: null,
              line_total_cents: 350,
              category: null,
              applies_to: null,
            },
          ],
        },
      },
    ]);
  });

  it("lets a person answer an open review whose receipt failed", async () => {
    const requests = serveReview(review({ receipt_status: "failed" }));
    const user = userEvent.setup();
    renderApp("/review/7");
    await form();

    await user.click(screen.getByRole("button", { name: "Accept as read" }));

    await waitFor(() => {
      expect(requests.answers).toEqual([{ accept: true, force: false }]);
    });
  });

  it("says a review was already answered", async () => {
    serveReview(review({ receipt_status: "processing", answer_queued: true }));

    renderApp("/review/7");

    expect(
      await screen.findByRole("heading", { name: "Already answered" }),
    ).toBeInTheDocument();
  });

  it("shows not found for an unknown review", async () => {
    server.use(
      http.get("/api/reviews/:reviewId", () =>
        HttpResponse.json(
          {
            ...inconsistent,
            type: "/problems/not-found",
            status: 404,
            errors: null,
          },
          { status: 404, headers: PROBLEM_HEADERS },
        ),
      ),
    );

    renderApp("/review/424242");

    expect(
      await screen.findByRole("heading", { name: "Review not found" }),
    ).toBeInTheDocument();
  });
});
