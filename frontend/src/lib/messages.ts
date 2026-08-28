import type { UIMessage } from "ai";

// Worker tools live in the Python backend. Their implementations are migration
// stubs until the Vercel Sandbox control plane lands.
export type HatcheryTools = {
  create_sandbox: {
    input: { repos?: string[]; title?: string };
    output: unknown;
  };
  list_sandboxes: {
    input: Record<string, never>;
    output: unknown;
  };
  create_subagent: {
    input: { sandbox_id?: string; task?: string; model?: string };
    output: unknown;
  };
};

export type ChatUIMessage = UIMessage<
  { origin?: "slack" },
  {
    reload: unknown;
    "space-assignment": {
      state: "assigning" | "assigned";
      space_id?: string;
      space_name?: string;
    };
  },
  HatcheryTools
>;

export type ChatMessagePart = ChatUIMessage["parts"][number];

export type ChatToolPart = Extract<ChatMessagePart, { toolCallId: string }>;

// Ported from seal (lib/messages.ts): drop steps invalidated by a mid-stream
// reconnect (data-reload), and dedupe tool parts replayed by AI SDK v7 —
// keep the last occurrence, which carries the freshest state.
export function getFreshParts<T extends { type: string }>(parts: T[]): T[] {
  const freshParts: T[] = [];

  for (const part of parts) {
    freshParts.push(part);
    if (part.type == "data-reload") {
      const reloadIndex = freshParts.findLastIndex(
        (part) => part.type === "step-start",
      );
      freshParts.splice(reloadIndex + 1);
    }
  }

  const isToolLike = (part: T) =>
    part.type.startsWith("tool-") || part.type === "dynamic-tool";
  return freshParts.filter((part, index) => {
    if (!isToolLike(part)) return true;
    const id = (part as { toolCallId?: string }).toolCallId;
    return (
      freshParts.findLastIndex(
        (other) =>
          isToolLike(other) &&
          (other as { toolCallId?: string }).toolCallId === id,
      ) === index
    );
  });
}
