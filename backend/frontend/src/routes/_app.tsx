import { createFileRoute, Outlet } from "@tanstack/react-router";

import { AppShell } from "@/app/app-shell";

export const Route = createFileRoute("/_app")({
  component: AppLayout,
});

function AppLayout() {
  return (
    <>
      <AppShell />
      <Outlet />
    </>
  );
}
