import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/_app/chats/$chatId")({
  component: RoutePlaceholder,
});

function RoutePlaceholder() {
  return null;
}
