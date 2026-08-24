import type { UIMessage } from "ai";

// Tools live in the Python backend, so the map is written by hand
// (InferUITools needs TypeScript tool definitions). Unknown tools still
// render through the generic ToolPart fallback.
export type FabricatorTools = {
  launch_coder: {
    input: { task?: string };
    output:
      | { launch_id: string; task_id: string; state: string }
      | string;
  };
};

export type ChatUIMessage = UIMessage<
  { origin?: "slack" },
  { reload: unknown },
  FabricatorTools
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
