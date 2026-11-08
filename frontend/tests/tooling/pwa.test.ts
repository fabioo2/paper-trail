// @vitest-environment node
import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

import { frontendPath } from "./paths";

interface ManifestIcon {
  src: string;
  sizes: string;
  type?: string;
  purpose?: string;
}

interface Manifest {
  name: string;
  short_name: string;
  start_url: string;
  scope: string;
  display: string;
  theme_color: string;
  background_color: string;
  icons: ManifestIcon[];
}

async function manifest(): Promise<Manifest> {
  return JSON.parse(
    await readFile(frontendPath("public/manifest.webmanifest"), "utf8"),
  ) as Manifest;
}

async function indexDocument(): Promise<Document> {
  return new JSDOM(await readFile(frontendPath("index.html"), "utf8")).window
    .document;
}

function publicFile(href: string): string {
  return frontendPath("public", href.replace(/^\//, ""));
}

async function pngSize(path: string): Promise<string> {
  const bytes = await readFile(path);
  expect(bytes.subarray(0, 8)).toEqual(
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  );
  return `${String(bytes.readUInt32BE(16))}x${String(bytes.readUInt32BE(20))}`;
}

async function sourceFiles(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true, recursive: true });
  return entries
    .filter((entry) => entry.isFile() && /\.(ts|tsx)$/.test(entry.name))
    .map((entry) => join(entry.parentPath, entry.name));
}

describe("PWA manifest", () => {
  it("S0-AC-33: has the required fields", async () => {
    const m = await manifest();

    expect(m.name).toBe("Paper Trail");
    expect(m.short_name).toBe("Paper Trail");
    expect(m.start_url).toBe("/scan");
    expect(m.scope).toBe("/");
    expect(m.display).toBe("standalone");
    expect(m.theme_color).toMatch(/^#[0-9a-f]{3,8}$/i);
    expect(m.background_color).toMatch(/^#[0-9a-f]{3,8}$/i);
  });

  it("S0-AC-33: declares 192 px, 512 px and a maskable icon", async () => {
    const { icons } = await manifest();

    expect(icons.map((icon) => icon.sizes)).toEqual(
      expect.arrayContaining(["192x192", "512x512"]),
    );
    expect(
      icons.some((icon) => icon.purpose?.split(" ").includes("maskable")),
    ).toBe(true);
  });

  it("S0-AC-33: every icon exists and has the declared pixel size", async () => {
    for (const icon of (await manifest()).icons) {
      const path = publicFile(icon.src);
      expect(existsSync(path), icon.src).toBe(true);
      expect(await pngSize(path), icon.src).toBe(icon.sizes);
    }
  });
});

describe("index.html", () => {
  it("S0-AC-33: links the manifest and an existing apple-touch-icon", async () => {
    const document = await indexDocument();
    const manifestLink = document.querySelector('link[rel="manifest"]');
    const touchIcon = document.querySelector('link[rel="apple-touch-icon"]');

    expect(manifestLink?.getAttribute("href")).toBe("/manifest.webmanifest");
    const touchHref = touchIcon?.getAttribute("href") ?? "";
    expect(existsSync(publicFile(touchHref)), touchHref).toBe(true);
    const [width, height] = (await pngSize(publicFile(touchHref)))
      .split("x")
      .map(Number);
    expect(width).toBe(height);
    expect(width).toBeGreaterThanOrEqual(180);
  });

  it("S0-AC-33: is installable full screen on iOS and honours safe areas", async () => {
    const document = await indexDocument();
    const meta = (name: string): string | null | undefined =>
      document.querySelector(`meta[name="${name}"]`)?.getAttribute("content");

    expect(meta("viewport")).toContain("viewport-fit=cover");
    expect(meta("theme-color")).toBeTruthy();
    expect(
      meta("apple-mobile-web-app-capable") ?? meta("mobile-web-app-capable"),
    ).toBe("yes");
    expect(document.querySelector("div#root")).not.toBeNull();
  });

  it("S0-AC-33: no service worker is registered (no offline mode without HTTPS)", async () => {
    for (const file of await sourceFiles(frontendPath("src"))) {
      if (file.endsWith(".test.ts") || file.endsWith(".test.tsx")) continue;
      expect(await readFile(file, "utf8"), file).not.toContain("serviceWorker");
    }
  });
});
