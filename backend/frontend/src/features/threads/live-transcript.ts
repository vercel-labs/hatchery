import { getToolName, isToolUIPart } from "ai";

import type { Message } from "@/lib/api-types";
import { getFreshParts, type ChatUIMessage } from "@/lib/messages";

// The durable transcript arrives as stored model messages (see
// GET /api/chats/{id}/transcript). The in-flight turn arrives through Hatchery's
// UI message stream (`useChat`). This maps that live UI message onto the same
// shape: one model message per step, with tool calls and their results.
export function liveMessages(messages: ChatUIMessage[]): Message[] {
  const converted: Message[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      converted.push({
        role: "user",
        id: message.id,
        request_id: message.id,
        author: message.metadata?.author,
        origin: message.metadata?.origin,
        parts: message.parts.flatMap((part, index) =>
          part.type === "text"
            ? [{ kind: "text", id: `${message.id}:${index}`, text: part.text }]
            : [],
        ),
      });
      continue;
    }
    let step: Message = { role: "assistant", id: message.id, parts: [] };
    const results: Message = { role: "tool", parts: [] };
    const flush = () => {
      if (step.parts?.length) converted.push(step);
      if (results.parts?.length) converted.push({ ...results });
      results.parts = [];
    };
    getFreshParts(message.parts).forEach((part, index) => {
      const id = `${message.id}:${index}`;
      if (part.type === "step-start" && step.parts?.length) {
        flush();
        step = { role: "assistant", id: `${message.id}:${index}`, parts: [] };
      } else if (part.type === "text" && part.text) {
        step.parts!.push({
          kind: "text",
          id,
          text: part.text,
          streaming: part.state === "streaming",
        });
      } else if (part.type === "data-agent-assignment") {
        flush();
        converted.push({
          role: "assistant",
          source: "signal",
          parts: [
            {
              kind: "text",
              id,
              text:
                part.data.state === "assigning"
                  ? "Assigning an agent…"
                  : `Assigned ${part.data.agent_name ?? "agent"}`,
            },
          ],
        });
      } else if (isToolUIPart(part)) {
        const name = getToolName(part);
        step.parts!.push({
          kind: "tool_call",
          id: `${id}:call`,
          tool_call_id: part.toolCallId,
          tool_name: name,
          tool_args: JSON.stringify(part.input ?? {}),
        });
        if (part.state === "output-available" && !part.preliminary) {
          results.parts!.push({
            kind: "tool_result",
            id: `${id}:result`,
            tool_call_id: part.toolCallId,
            tool_name: name,
            result: part.output,
            result_kind: "json",
          });
        } else if (
          part.state === "output-error" ||
          part.state === "output-denied"
        ) {
          results.parts!.push({
            kind: "tool_result",
            id: `${id}:result`,
            tool_call_id: part.toolCallId,
            tool_name: name,
            result:
              part.state === "output-error" ? part.errorText : "Tool denied",
            result_kind: "error",
          });
        }
      }
    });
    flush();
  }
  return converted;
}
