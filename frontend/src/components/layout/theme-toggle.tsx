import { type LucideIcon, Monitor, Moon, Sun } from "lucide-react";

import { useTheme } from "@/components/theme/theme-context";
import { Button } from "@/components/ui/button";
import { nextPreference, type ThemePreference } from "@/lib/theme";

const ICONS: Record<ThemePreference, LucideIcon> = {
  system: Monitor,
  light: Sun,
  dark: Moon,
};

/** Cycles the theme: system, light, dark, system. */
export function ThemeToggle() {
  const { preference, setPreference } = useTheme();
  const next = nextPreference(preference);
  const Icon = ICONS[preference];

  return (
    <Button
      variant="ghost"
      size="icon"
      className="size-11"
      aria-label={`Theme: ${preference}. Switch to ${next}`}
      onClick={() => {
        setPreference(next);
      }}
    >
      <Icon aria-hidden="true" />
    </Button>
  );
}
