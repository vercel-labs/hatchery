import { createRootRoute, Outlet } from "@tanstack/react-router";

import { TooltipProvider } from "@/components/ui/tooltip";

export const Route = createRootRoute({
  component: RootLayout,
  notFoundComponent: NotFound,
});

function RootLayout() {
  return (
    <TooltipProvider>
      <Outlet />
    </TooltipProvider>
  );
}

function NotFound() {
  return (
    <main className="flex h-svh items-center justify-center p-6 text-sm text-muted-foreground">
      Page not found.
    </main>
  );
}
