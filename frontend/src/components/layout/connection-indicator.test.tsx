import { screen, waitFor } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ConnectionIndicator } from "@/components/layout/connection-indicator";
import { healthyResponse, type ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderWithProviders } from "@/test/render";

const SKELETON = '[data-slot="skeleton"]';

const notReady: ProblemDetail = {
  type: "/problems/internal",
  title: "Internal Server Error",
  status: 500,
  detail: null,
  instance: null,
  errors: null,
};

describe("ConnectionIndicator", () => {
  it("S0-AC-30: shows a loading skeleton until /api/health answers", async () => {
    server.use(
      http.get("/api/health", async () => {
        await delay("infinite");
        return HttpResponse.json(healthyResponse);
      }),
    );

    const { container } = renderWithProviders(<ConnectionIndicator />);

    await waitFor(() => {
      expect(container.querySelector(SKELETON)).toBeInTheDocument();
    });
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
    expect(screen.queryByText("Offline")).not.toBeInTheDocument();
  });

  it("S0-AC-30: shows Connected when GET /api/health returns 200", async () => {
    const { container } = renderWithProviders(<ConnectionIndicator />);

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(container.querySelector(SKELETON)).not.toBeInTheDocument();
  });

  it.each([
    ["503 problem", () => HttpResponse.json(notReady, { status: 503 })],
    ["500 plain text", () => new HttpResponse("boom", { status: 500 })],
    ["network error", () => HttpResponse.error()],
  ])("S0-AC-30: shows Offline on %s", async (_label, resolver) => {
    server.use(http.get("/api/health", resolver));

    renderWithProviders(<ConnectionIndicator />);

    expect(await screen.findByText("Offline")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });

  it("S0-AC-30: queries the same-origin health endpoint through the API client", async () => {
    const requests: URL[] = [];
    server.use(
      http.get("/api/health", ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json(healthyResponse);
      }),
    );

    renderWithProviders(<ConnectionIndicator />);

    await screen.findByText("Connected");
    expect(requests).toHaveLength(1);
    expect(requests[0]?.origin).toBe(window.location.origin);
    expect(requests[0]?.pathname).toBe("/api/health");
  });

  it("S0-AC-30: the status is announced to assistive technology", async () => {
    renderWithProviders(<ConnectionIndicator />);

    const status = await screen.findByText("Connected");
    expect(status).toHaveClass("sr-only");
  });
});
