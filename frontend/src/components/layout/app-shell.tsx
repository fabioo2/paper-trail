import type { ReactNode } from "react";

import { BottomTabBar } from "@/components/layout/bottom-tab-bar";
import { TopBar } from "@/components/layout/top-bar";

/** Top bar, a scrollable main region and the bottom tab bar. */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <TopBar />
      <main className="mx-auto w-full max-w-2xl flex-1 pb-[calc(var(--tab-bar-height)+var(--safe-bottom))]">
        {children}
      </main>
      <BottomTabBar />
    </div>
  );
}
