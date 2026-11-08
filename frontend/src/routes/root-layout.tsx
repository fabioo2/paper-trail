import { Outlet } from "@tanstack/react-router";

import { AppShell } from "@/components/layout/app-shell";

export function RootLayout() {
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  );
}
