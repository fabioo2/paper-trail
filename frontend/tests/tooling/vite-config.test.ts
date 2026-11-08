// @vitest-environment node
import { readFile } from "node:fs/promises";

import { loadConfigFromFile, type UserConfig } from "vite";
import { describe, expect, it } from "vitest";

import { FRONTEND_ROOT, frontendPath } from "./paths";

async function viteConfig(): Promise<UserConfig> {
  const loaded = await loadConfigFromFile(
    { command: "serve", mode: "development" },
    frontendPath("vite.config.ts"),
    FRONTEND_ROOT,
  );
  if (!loaded) throw new Error("vite.config.ts not found");
  return loaded.config;
}

function proxyTarget(entry: unknown): unknown {
  return typeof entry === "string"
    ? entry
    : (entry as { target?: unknown } | undefined)?.target;
}

describe("vite configuration", () => {
  it("S0-AC-40: the dev server proxies /api to the web service", async () => {
    const config = await viteConfig();

    expect(proxyTarget(config.server?.proxy?.["/api"])).toBe("http://web:8000");
  });

  it("S0-AC-40: the dev server listens on 5173 for the compose port mapping", async () => {
    const config = await viteConfig();

    expect(config.server?.port ?? 5173).toBe(5173);
  });

  it("S0-AC-40: the API base URL is a build-time VITE_ variable, not hard-coded", async () => {
    const client = await readFile(frontendPath("src/api/client.ts"), "utf8");

    expect(client).toContain("import.meta.env.VITE_API_BASE_URL");
    expect(client).not.toMatch(/https?:\/\//);
  });

  it("S0-AC-40: VITE_API_BASE_URL is typed in the Vite env declarations", async () => {
    const declarations = await readFile(
      frontendPath("src/vite-env.d.ts"),
      "utf8",
    );

    expect(declarations).toMatch(/VITE_API_BASE_URL\??\s*:\s*string/);
  });

  it("S0-AC-27: vitest runs in jsdom with the shared setup file", async () => {
    const config = (await viteConfig()) as UserConfig & {
      test?: { environment?: string; setupFiles?: string | string[] };
    };

    expect(config.test?.environment).toBe("jsdom");
    expect([config.test?.setupFiles].flat().join(" ")).toContain(
      "src/test/setup.ts",
    );
  });
});
