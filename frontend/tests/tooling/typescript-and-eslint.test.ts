// @vitest-environment node
import { ESLint } from "eslint";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import { FRONTEND_ROOT, frontendPath } from "./paths";

function compilerOptions(configFile: string): ts.CompilerOptions {
  const parsed = ts.getParsedCommandLineOfConfigFile(
    frontendPath(configFile),
    undefined,
    {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: (diagnostic) => {
        throw new Error(
          ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
        );
      },
    },
  );
  if (!parsed) throw new Error(`cannot parse ${configFile}`);
  return parsed.options;
}

function isEnabled(setting: unknown): boolean {
  const severity: unknown = Array.isArray(setting) ? setting[0] : setting;
  return severity !== 0 && severity !== "off" && severity !== undefined;
}

describe("TypeScript configuration", () => {
  it.each(["tsconfig.app.json", "tsconfig.node.json"])(
    "S0-AC-27: %s is strict with noUncheckedIndexedAccess and exactOptionalPropertyTypes",
    (configFile) => {
      const options = compilerOptions(configFile);

      expect(options.strict).toBe(true);
      expect(options.noUncheckedIndexedAccess).toBe(true);
      expect(options.exactOptionalPropertyTypes).toBe(true);
    },
  );

  it("S0-AC-27: the root tsconfig references the app and node projects (tsc -b)", () => {
    const { config } = ts.readConfigFile(
      frontendPath("tsconfig.json"),
      (path) => ts.sys.readFile(path),
    ) as {
      config: { references?: { path: string }[] };
    };
    const references = (config.references ?? []).map(
      (reference) => reference.path,
    );

    expect(references).toEqual(
      expect.arrayContaining(["./tsconfig.app.json", "./tsconfig.node.json"]),
    );
  });

  it("S0-AC-27: the app project resolves @/ to src/", () => {
    const options = compilerOptions("tsconfig.app.json");

    expect(options.paths?.["@/*"]).toEqual(["./src/*"]);
  });
});

describe("ESLint configuration", () => {
  const eslint = new ESLint({ cwd: FRONTEND_ROOT });

  it.each([
    // only in typescript-eslint strict-type-checked (not recommended/strict)
    "@typescript-eslint/no-confusing-void-expression",
    "@typescript-eslint/no-unnecessary-condition",
    // type-checked rules
    "@typescript-eslint/no-unsafe-assignment",
    "@typescript-eslint/no-floating-promises",
    // react-hooks
    "react-hooks/rules-of-hooks",
    "react-hooks/exhaustive-deps",
    // jsx-a11y
    "jsx-a11y/alt-text",
    "jsx-a11y/anchor-is-valid",
    // react-refresh
    "react-refresh/only-export-components",
  ])("S0-AC-27: %s is enabled for application code", async (rule) => {
    const config = (await eslint.calculateConfigForFile("src/main.tsx")) as {
      rules?: Record<string, unknown>;
    };

    expect(isEnabled(config.rules?.[rule])).toBe(true);
  });

  it("S0-AC-27: typed linting is wired to the TypeScript project", async () => {
    const config = (await eslint.calculateConfigForFile("src/main.tsx")) as {
      languageOptions?: {
        parserOptions?: { projectService?: unknown; project?: unknown };
      };
    };
    const parserOptions = config.languageOptions?.parserOptions ?? {};

    expect(
      Boolean(parserOptions.projectService) || Boolean(parserOptions.project),
    ).toBe(true);
  });

  it.each([
    "scripts/generate-icons.mjs",
    "scripts/example.cjs",
    "eslint.config.js",
  ])("S0-AC-27: Node script %s is linted with Node globals", async (file) => {
    expect(await eslint.isPathIgnored(file)).toBe(false);
    const config = (await eslint.calculateConfigForFile(file)) as
      | {
          rules?: Record<string, unknown>;
          languageOptions?: { globals?: Record<string, unknown> };
        }
      | undefined;

    expect(isEnabled(config?.rules?.["no-undef"])).toBe(true);
    expect(config?.languageOptions?.globals).toHaveProperty("process");
  });

  it("S0-AC-27: generated files are not linted", async () => {
    expect(await eslint.isPathIgnored("dist/assets/index.js")).toBe(true);
    expect(await eslint.isPathIgnored("src/api/schema.d.ts")).toBe(true);
  });
});
