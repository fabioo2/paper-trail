// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { frontendPath } from "./paths";

const MONEY_MODULE = "src/features/receipts/money.ts";

/** Every non-test, non-generated source file under src/. */
async function sourceFiles(): Promise<string[]> {
  const entries = await readdir(frontendPath("src"), {
    recursive: true,
    withFileTypes: true,
  });
  return entries
    .filter((entry) => entry.isFile() && /\.tsx?$/.test(entry.name))
    .map((entry) =>
      relative(frontendPath(), join(entry.parentPath, entry.name)),
    )
    .filter(
      (path) =>
        !/\.test\.tsx?$/.test(path) &&
        !path.startsWith("src/test/") &&
        path !== "src/api/schema.d.ts",
    );
}

describe("money formatting", () => {
  it("S1-AC-27: no source file formats money with toFixed", async () => {
    const offenders: string[] = [];
    for (const path of await sourceFiles()) {
      if ((await readFile(frontendPath(path), "utf8")).includes(".toFixed(")) {
        offenders.push(path);
      }
    }

    expect(offenders).toEqual([]);
  });

  it("S1-AC-27: currency formatting lives only in features/receipts/money.ts", async () => {
    const formatters: string[] = [];
    for (const path of await sourceFiles()) {
      const source = await readFile(frontendPath(path), "utf8");
      if (/style:\s*["']currency["']/.test(source)) formatters.push(path);
    }

    expect(formatters).toEqual([MONEY_MODULE]);
  });

  it("S1-AC-27: the receipts screens use formatMoney", async () => {
    for (const path of [
      "src/features/receipts/receipt-list.tsx",
      "src/features/receipts/receipt-detail.tsx",
    ]) {
      const source = await readFile(frontendPath(path), "utf8");
      expect(source, path).toMatch(/\bformatMoney\b/);
    }
  });
});
