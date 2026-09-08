import { getToolName, isToolUIPart } from "ai";

import {
  getFreshParts,
  type ChatMessagePart,
  type ChatUIMessage,
} from "./messages.ts";

export type SharedThread = {
  id: string;
  provider: "slack" | "github";
  label: string;
  url: string;
  text: string;
  tool_call_id: string | null;
  // External root message ID, never a UI message anchor.
  message_id: string | null;
};

export type SharedThreadMessage = Omit<ChatUIMessage, "parts"> & {
  parts: (ChatMessagePart | { type: "shared-thread"; sharing: SharedThread })[];
};

function isSharedThread(value: unknown): value is SharedThread {
  if (!value || typeof value !== "object") return false;
  const thread = value as Record<string, unknown>;
  return (
    typeof thread.id === "string" &&
    (thread.provider === "slack" || thread.provider === "github") &&
    typeof thread.label === "string" &&
    typeof thread.url === "string" &&
    typeof thread.text === "string" &&
    (thread.tool_call_id === null || typeof thread.tool_call_id === "string") &&
    (thread.message_id === null || typeof thread.message_id === "string")
  );
}

// One timeline for streamed results and durable records. Deduplicate by record
// ID, not destination or external root ID: a chat can bind multiple threads.
export function sharingTimeline(messages: ChatUIMessage[], saved: SharedThread[]) {
  const fresh = messages.map((message) => ({
    ...message,
    parts: getFreshParts(message.parts),
  }));
  const threads = new Map<string, SharedThread>();
  const resultAnchors = new Map<string, string>();
  const sharedCalls = new Set<string>();
  const toolCalls = new Set<string>();

  for (const message of fresh) {
    if (message.role !== "assistant") continue;
    for (const part of message.parts) {
      if (!isToolUIPart(part)) continue;
      toolCalls.add(part.toolCallId);
      if (
        getToolName(part) !== "start_shared_thread" ||
        part.state !== "output-available" ||
        !part.output ||
        typeof part.output !== "object" ||
        !("status" in part.output) ||
        part.output.status !== "sent" ||
        !("sharing" in part.output) ||
        !isSharedThread(part.output.sharing)
      ) continue;
      const sharing = part.output.sharing;
      threads.set(sharing.id, sharing);
      sharedCalls.add(part.toolCallId);
      resultAnchors.set(sharing.id, part.toolCallId);
    }
  }

  // Saved records are authoritative, including when a failed turn lost output.
  for (const sharing of saved) threads.set(sharing.id, sharing);
  const anchored = new Map<string, SharedThread[]>();
  const fallback: SharedThread[] = [];
  for (const sharing of threads.values()) {
    const callId = resultAnchors.get(sharing.id) ?? sharing.tool_call_id;
    if (callId && toolCalls.has(callId)) {
      const atCall = anchored.get(callId) ?? [];
      atCall.push(sharing);
      anchored.set(callId, atCall);
    } else {
      fallback.push(sharing);
    }
  }

  const renderedCalls = new Set<string>();
  const timeline: SharedThreadMessage[] = fresh.map((message) => ({
    ...message,
    parts: message.parts.flatMap<SharedThreadMessage["parts"][number]>((part) => {
      if (message.role !== "assistant" || !isToolUIPart(part)) return [part];
      const atCall = anchored.get(part.toolCallId);
      if (!atCall) return sharedCalls.has(part.toolCallId) ? [] : [part];
      // Replayed calls must not show a second notification or a JSON card.
      if (renderedCalls.has(part.toolCallId)) return [];
      renderedCalls.add(part.toolCallId);
      return atCall.map((sharing) => ({ type: "shared-thread", sharing }));
    }),
  }));

  return { messages: timeline, threads: [...threads.values()], fallback };
}
