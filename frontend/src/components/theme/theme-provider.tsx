import {
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import {
  ThemeContext,
  type ThemeState,
} from "@/components/theme/theme-context";
import {
  applyTheme,
  DARK_SCHEME_QUERY,
  readStoredPreference,
  resolveTheme,
  storePreference,
  type ThemePreference,
} from "@/lib/theme";

function subscribeToColorScheme(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_SCHEME_QUERY);
  query.addEventListener("change", onChange);
  return () => {
    query.removeEventListener("change", onChange);
  };
}

function prefersDark(): boolean {
  return window.matchMedia(DARK_SCHEME_QUERY).matches;
}

/** Owns the theme preference, follows OS changes live and applies the result to <html>. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] =
    useState<ThemePreference>(readStoredPreference);
  const osPrefersDark = useSyncExternalStore(
    subscribeToColorScheme,
    prefersDark,
  );
  const resolved = resolveTheme(preference, osPrefersDark);

  useLayoutEffect(() => {
    applyTheme(resolved);
  }, [resolved]);

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    storePreference(next);
  }, []);

  const value = useMemo<ThemeState>(
    () => ({ preference, resolved, setPreference }),
    [preference, resolved, setPreference],
  );

  return <ThemeContext value={value}>{children}</ThemeContext>;
}
