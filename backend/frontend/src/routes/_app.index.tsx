import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_app/")({
  component: RoutePlaceholder,
});

function RoutePlaceholder() {
  return null;
}
