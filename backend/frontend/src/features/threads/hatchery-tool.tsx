import { ToolPart } from "@/components/parts/tool-part";
import type { ChatToolPart } from "@/lib/messages";
import type { ToolItem } from "./transcript-model";

function input(args: unknown): unknown {
  if (typeof args !== "string") return args ?? {};
  try {
    return JSON.parse(args);
  } catch {
    return args;
  }
}

// Hatchery's own tools (fx subagents, extra sandboxes) keep Hatchery's tool part.
export function HatcheryToolCard({ item }: { item: ToolItem }) {
  const failed = item.result?.result_kind === "error";
  const part = {
    type: "dynamic-tool",
    toolName: item.name,
    toolCallId: String(item.call?.tool_call_id ?? item.key),
    input: input(item.call?.tool_args),
    ...(item.result
      ? failed
        ? { state: "output-error", errorText: String(item.result.result) }
        : { state: "output-available", output: item.result.result }
      : { state: "input-available" }),
  } as ChatToolPart;
  return (
    <div
      data-tool-name={item.name}
      data-tool-state={part.state}
      className="flex w-full min-w-0 flex-col gap-1.5 py-0.5"
    >
      <ToolPart part={part} />
    </div>
  );
}
