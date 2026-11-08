import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { setPrefersDark } from "@/test/match-media";
import { renderApp } from "@/test/render";

function isDark(): boolean {
  return document.documentElement.classList.contains("dark");
}

async function themeButton(): Promise<HTMLElement> {
  return screen.findByRole("button", { name: /theme/i });
}

function addThemeColorMeta(): HTMLMetaElement {
  const meta = document.createElement("meta");
  meta.name = "theme-color";
  meta.content = "#ffffff";
  document.head.append(meta);
  return meta;
}

describe("theme", () => {
  it("S0-AC-31: with no stored preference, dark follows prefers-color-scheme (dark)", async () => {
    setPrefersDark(true);
    renderApp("/scan");
    await themeButton();

    expect(isDark()).toBe(true);
  });

  it("S0-AC-31: with no stored preference, dark follows prefers-color-scheme (light)", async () => {
    setPrefersDark(false);
    renderApp("/scan");
    await themeButton();

    expect(isDark()).toBe(false);
  });

  it("S0-AC-31: system mode follows OS changes live", async () => {
    renderApp("/scan");
    await themeButton();

    act(() => {
      setPrefersDark(true);
    });
    expect(isDark()).toBe(true);

    act(() => {
      setPrefersDark(false);
    });
    expect(isDark()).toBe(false);
  });

  it("S0-AC-31: the toggle cycles system -> light -> dark -> system", async () => {
    const user = userEvent.setup();
    renderApp("/scan");
    const button = await themeButton();

    await user.click(button); // light: ignores the OS
    act(() => {
      setPrefersDark(true);
    });
    expect(isDark()).toBe(false);

    await user.click(button); // dark: ignores the OS
    act(() => {
      setPrefersDark(false);
    });
    expect(isDark()).toBe(true);

    await user.click(button); // system again: follows the OS
    expect(isDark()).toBe(false);
    act(() => {
      setPrefersDark(true);
    });
    expect(isDark()).toBe(true);
  });

  it("S0-AC-31: the chosen theme persists across reloads", async () => {
    const user = userEvent.setup();
    const first = renderApp("/scan");
    const button = await themeButton();
    await user.click(button); // light
    await user.click(button); // dark
    first.unmount();
    document.documentElement.className = "";

    renderApp("/scan");
    await themeButton();

    expect(isDark()).toBe(true);
  });

  it("S0-AC-31: theme-color meta follows the applied theme", async () => {
    const user = userEvent.setup();
    const meta = addThemeColorMeta();
    renderApp("/scan");
    const button = await themeButton();

    await user.click(button); // light
    const light = meta.content;
    await user.click(button); // dark
    const dark = meta.content;

    expect(light).not.toBe("");
    expect(dark).not.toBe("");
    expect(dark).not.toBe(light);
  });

  it("S0-AC-31: when localStorage throws, the app renders and the toggle works for the session", async () => {
    const denied = (): never => {
      throw new DOMException("The operation is insecure.", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(denied);
    const user = userEvent.setup();

    renderApp("/scan");
    const button = await themeButton();
    expect(
      screen.getByRole("navigation", { name: "Primary" }),
    ).toBeInTheDocument();

    await user.click(button); // light
    await user.click(button); // dark
    expect(isDark()).toBe(true);
  });
});
