import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "@/test/render";

const TABS = [
  { name: "Scan", href: "/scan" },
  { name: "Receipts", href: "/receipts" },
  { name: "Search", href: "/search" },
  { name: "Review", href: "/review" },
] as const;

async function primaryNav(): Promise<HTMLElement> {
  return screen.findByRole("navigation", { name: "Primary" });
}

describe("BottomTabBar", () => {
  it("S0-AC-28: renders a nav labelled Primary with exactly four links in order", async () => {
    renderApp("/scan");

    const links = within(await primaryNav()).getAllByRole("link");

    expect(links).toHaveLength(4);
    TABS.forEach((tab, index) => {
      expect(links[index]).toHaveAccessibleName(tab.name);
    });
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      TABS.map((tab) => tab.href),
    );
  });

  it("S0-AC-28: every tab has an accessible name (icon + label)", async () => {
    renderApp("/scan");
    const nav = await primaryNav();

    for (const tab of TABS) {
      expect(
        within(nav).getByRole("link", { name: tab.name }),
      ).toBeInTheDocument();
    }
  });

  it.each(TABS)(
    "S0-AC-28: only the $name tab has aria-current=page on $href",
    async ({ name, href }) => {
      renderApp(href);
      const nav = await primaryNav();

      const current = await within(nav).findByRole("link", { current: "page" });
      expect(current).toHaveAccessibleName(name);
      for (const link of within(nav).getAllByRole("link")) {
        if (link !== current)
          expect(link).not.toHaveAttribute("aria-current", "page");
      }
    },
  );

  it("S0-AC-28: a nested path keeps its tab current", async () => {
    renderApp("/review/anything");
    const nav = await primaryNav();

    expect(
      await within(nav).findByRole("link", { current: "page" }),
    ).toHaveAccessibleName("Review");
  });
});
