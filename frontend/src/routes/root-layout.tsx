import { Outlet } from "@tanstack/react-router";

import { AppShell } from "@/components/layout/app-shell";
import { ReadingWatcher } from "@/features/receipts/reading-watcher";

export function RootLayout() {
  return (
    <AppShell>
      <Outlet />
      <ReadingWatcher />
    </AppShell>
  );
}
