import { ConnectionIndicator } from "@/components/layout/connection-indicator";
import { ThemeToggle } from "@/components/layout/theme-toggle";

export function TopBar() {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/90 pt-safe-top backdrop-blur">
      <div className="mx-auto flex h-14 max-w-2xl items-center justify-between pr-[max(0.5rem,var(--safe-right))] pl-[max(1rem,var(--safe-left))]">
        <span className="text-base font-semibold tracking-tight">
          Paper Trail
        </span>
        <div className="flex items-center gap-1">
          <ConnectionIndicator />
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
