import {
  Archive,
  Check,
  Ellipsis,
  GitFork,
  Send,
  SquareTerminal,
  Wrench,
  X,
} from "lucide-react";
import type { Thread } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { threadActivity } from "./thread-activity";

export function threadStatusBadge(thread: Thread) {
  const activity = threadActivity(thread);
  const status = thread.activity?.status ?? thread.status;
  if (thread.archived || status === "archived")
    return {
      kind: "archived",
      label: "Archived",
      icon: Archive,
      tone: "bg-zinc-500 text-white",
    };
  if (
    status === "failed" ||
    status === "cancelled" ||
    thread.task_status === "cancelled" ||
    thread.task_status === "rejected"
  )
    return {
      kind: "failed",
      label:
        status === "failed"
          ? "Failed"
          : thread.task_status === "rejected"
            ? "Rejected"
            : "Cancelled",
      icon: X,
      tone: "bg-red-500 text-white",
    };
  if (activity.tone === "attention")
    return {
      kind: "attention",
      label: activity.label,
      icon: null,
      tone: "bg-amber-400 text-amber-950",
    };
  if (activity.working) {
    const tool = thread.activity?.running_tool?.tool_name;
    const icon =
      tool === "bash"
        ? SquareTerminal
        : tool === "message_task" || tool === "message_parent"
          ? Send
          : tool === "delegate"
            ? GitFork
            : tool
              ? Wrench
              : Ellipsis;
    return {
      kind: "working",
      label: tool ? activity.label : "Thinking",
      icon,
      tone: "bg-blue-500 text-white",
    };
  }
  if (activity.tone === "error")
    return {
      kind: "attention",
      label: activity.label,
      icon: null,
      tone: "bg-amber-400 text-amber-950",
    };
  if (thread.parent_thread_id && thread.task_status === "completed")
    return {
      kind: "completed",
      label: "Completed",
      icon: Check,
      tone: "bg-emerald-500 text-white",
    };
  return null;
}

export function ThreadStatusBadge({
  badge,
}: {
  badge: NonNullable<ReturnType<typeof threadStatusBadge>>;
}) {
  const Icon = badge.icon;
  return (
    <span
      role="img"
      aria-label={`${badge.label} badge`}
      title={badge.label}
      className={cn(
        "absolute -end-1 -top-1 flex size-3.5 items-center justify-center rounded-full border-2 border-background",
        badge.tone,
      )}
    >
      {Icon ? (
        <Icon aria-hidden className="size-2" strokeWidth={2.5} />
      ) : (
        <span aria-hidden className="text-[10px] leading-none font-bold">
          !
        </span>
      )}
    </span>
  );
}
