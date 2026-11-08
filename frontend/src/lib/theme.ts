/**
 * Theme preference: follow the OS ("system") or force light or dark.
 * The preference is remembered in localStorage when the browser allows it; every storage access
 * is guarded because private modes and locked-down browsers throw on access.
 */
export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "paper-trail.theme";
export const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";

/** Browser chrome colour per theme; matches --background in src/styles/tokens.css. */
export const THEME_COLORS: Record<ResolvedTheme, string> = {
  light: "#fbfaf7",
  dark: "#16171b",
};

const NEXT_PREFERENCE: Record<ThemePreference, ThemePreference> = {
  system: "light",
  light: "dark",
  dark: "system",
};

function isThemePreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

export function nextPreference(current: ThemePreference): ThemePreference {
  return NEXT_PREFERENCE[current];
}

export function readStoredPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function storePreference(preference: ThemePreference): void {
  try {
    if (preference === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    // Storage is unavailable: the choice still applies for this session.
  }
}

export function resolveTheme(
  preference: ThemePreference,
  prefersDark: boolean,
): ResolvedTheme {
  if (preference === "system") return prefersDark ? "dark" : "light";
  return preference;
}

/** Apply a theme to the document: the `dark` class, native control colours and browser chrome. */
export function applyTheme(theme: ResolvedTheme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
  document
    .querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')
    .forEach((meta) => {
      meta.content = THEME_COLORS[theme];
    });
}
