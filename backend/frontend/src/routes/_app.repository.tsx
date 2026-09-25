import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_app/repository")({
  component: RoutePlaceholder,
});

function RoutePlaceholder() {
  return null;
}
