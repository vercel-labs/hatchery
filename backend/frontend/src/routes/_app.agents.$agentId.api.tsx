import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_app/agents/$agentId/api")({
  component: RoutePlaceholder,
});

function RoutePlaceholder() {
  return null;
}
