import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "@/test/render";

describe("router", () => {
  it("S0-AC-28: / redirects to /scan", async () => {
    const { router } = renderApp("/");

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/scan");
    });
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(
      within(nav).getByRole("link", { current: "page" }),
    ).toHaveAccessibleName("Scan");
  });

  it("S0-AC-28: an unknown route renders the not-found page with a link back to /scan", async () => {
    renderApp("/definitely/not/a/page");

    expect(
      await screen.findByRole("heading", { name: /not found/i }),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const homeLinks = screen
      .getAllByRole("link")
      .filter(
        (link) => !nav.contains(link) && link.getAttribute("href") === "/scan",
      );
    expect(homeLinks.length).toBeGreaterThanOrEqual(1);
  });

  it("S0-AC-28: the not-found page keeps the app shell (tab bar still usable)", async () => {
    renderApp("/nope");

    await screen.findByRole("heading", { name: /not found/i });
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("navigation", { name: "Primary" })).queryByRole(
        "link",
        {
          current: "page",
        },
      ),
    ).not.toBeInTheDocument();
  });
});
