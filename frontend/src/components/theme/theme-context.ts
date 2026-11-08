import { createContext, useContext } from "react";

import type { ResolvedTheme, ThemePreference } from "@/lib/theme";

export interface ThemeState {
  /** What the user chose. */
  preference: ThemePreference;
  /** What is applied right now ("system" resolved against the OS setting). */
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}

export const ThemeContext = createContext<ThemeState | null>(null);

export function useTheme(): ThemeState {
  const theme = useContext(ThemeContext);
  if (!theme) throw new Error("useTheme must be used inside <ThemeProvider>");
  return theme;
}
