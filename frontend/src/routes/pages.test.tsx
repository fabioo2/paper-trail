import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const PAGES = ["/scan", "/receipts", "/search", "/review"] as const;
const FORBIDDEN_COPY = /\b(todo|lorem|placeholder)\b/i;

async function pageHeading(): Promise<{
  main: HTMLElement;
  heading: HTMLElement;
}> {
  const main = await screen.findByRole("main");
  const [heading] = await within(main).findAllByRole("heading");
  if (!heading) throw new Error("page has no heading");
  return { main, heading };
}

describe("pages", () => {
  it.each(PAGES)(
    "S0-AC-29: %s renders an empty state with a heading and a description",
    async (path) => {
      renderApp(path);

      const { main, heading } = await pageHeading();
      const description = textOf(main).replace(textOf(heading), "").trim();

      expect(textOf(heading)).not.toBe("");
      expect(description.length).toBeGreaterThan(15);
    },
  );

  it.each([...PAGES, "/not-a-page"])(
    "S0-AC-29: %s contains no TODO, lorem or placeholder copy",
    async (path) => {
      renderApp(path);
      await pageHeading();

      expect(textOf(document.body)).not.toMatch(FORBIDDEN_COPY);
    },
  );

  it("S0-AC-29: each page has its own heading", async () => {
    const headings: string[] = [];
    for (const path of PAGES) {
      const { unmount } = renderApp(path);
      headings.push(textOf((await pageHeading()).heading));
      unmount();
    }

    expect(new Set(headings).size).toBe(PAGES.length);
  });
});
