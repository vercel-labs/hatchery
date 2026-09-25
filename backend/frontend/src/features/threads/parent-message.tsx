import { Send } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolItem } from "./transcript-model";
import { taskArguments } from "./task-conversation-model";
import { MarkdownText } from "./markdown-text";

// A subagent's message_parent call reads as one of its own chat messages, with
// only a small marker distinguishing it from operator-facing replies.
export function ParentMessage({ item }: { item: ToolItem }) {
  const args = taskArguments(item.call?.tool_args);
  const text = String(args.text ?? "");
  const failed = item.result?.result_kind === "error";
  const pending = !item.result;
  const stopped = pending && item.execution === "stopped";
  const sending = pending && !stopped;
  const label = failed
    ? "Failed to send to parent agent"
    : stopped
      ? "Stopped before sending to parent agent"
      : sending
        ? "Sending to parent agent"
        : "Sent to parent agent";
  const failure =
    failed && typeof item.result?.result === "string" ? item.result.result : "";
  return (
    <section
      aria-label="Message to parent agent"
      data-parent-message={
        failed ? "failed" : stopped ? "stopped" : sending ? "sending" : "sent"
      }
      className={cn(
        "min-w-0 space-y-1.5 border-l-2 pl-3.5",
        failed ? "border-destructive/40" : "border-border",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-1.5 text-[11px] font-medium",
          failed ? "text-destructive" : "text-muted-foreground",
        )}
      >
        <Send aria-hidden className="size-3 shrink-0" strokeWidth={1.75} />
        <span className={sending ? "thinking-shimmer" : undefined}>
          {label}
        </span>
      </div>
      {text ? <MarkdownText text={text} messageText /> : null}
      {failure ? (
        <p className="whitespace-pre-wrap wrap-anywhere text-xs leading-5 text-destructive">
          {failure}
        </p>
      ) : null}
    </section>
  );
}
