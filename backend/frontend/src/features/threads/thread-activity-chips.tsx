import {
  Clock3,
  GitFork,
  MessageCircle,
  Send,
  SquareTerminal,
  Wrench,
} from "lucide-react";
import type { Thread } from "@/lib/api-types";
import {
  pendingPrompts,
  runningCommand,
  threadToolCounts,
} from "./thread-activity";

export function ThreadActivityChips({ thread }: { thread: Thread }) {
  const activity = thread.activity;
  if (
    !activity ||
    !thread.live ||
    thread.archived ||
    activity.phase === "terminal"
  )
    return null;
  const tools = threadToolCounts(thread);
  const prompts = pendingPrompts(thread);
  const tool = activity.running_tool;
  const ToolIcon =
    tool?.tool_name === "bash"
      ? SquareTerminal
      : tool?.tool_name === "message_task"
        ? Send
        : tool?.tool_name === "delegate"
          ? GitFork
          : Wrench;
  const badges = [
    {
      key: "inbox",
      icon: MessageCircle,
      count: activity.mailbox_depth,
      label: `${activity.mailbox_depth} runtime ${activity.mailbox_depth === 1 ? "message" : "messages"}`,
      detail: "Incoming runtime messages",
    },
    {
      key: "prompts",
      icon: Send,
      count: prompts,
      label: `${prompts} ${prompts === 1 ? "prompt" : "prompts"} queued`,
      detail: "Messages accepted for the next turn",
    },
    {
      key: "tools",
      icon: tool ? ToolIcon : Wrench,
      count: tools.pending,
      label:
        tools.running && tools.queued
          ? `${tools.running} tool running and ${tools.queued} ${tools.queued === 1 ? "tool" : "tools"} queued`
          : tools.running
            ? "1 tool running"
            : `${tools.queued} ${tools.queued === 1 ? "tool" : "tools"} queued`,
      detail: tool
        ? `${tool.tool_name}: ${runningCommand(thread)}`
        : "Tools waiting to start",
    },
    {
      key: "schedules",
      icon: Clock3,
      count: activity.schedules.length,
      label: `${activity.schedules.length} scheduled`,
      detail: "Scheduled work",
    },
  ].filter((badge) => badge.count > 0);
  if (!badges.length) return null;
  return (
    <span
      className="flex shrink-0 flex-wrap items-center gap-1"
      data-thread-badges
    >
      {badges.map(({ key, icon: Icon, count, label, detail }) => (
        <span
          key={key}
          aria-label={label}
          title={`${label} · ${detail}`}
          className="inline-flex h-5 shrink-0 items-center gap-1 rounded-full bg-blue-500/10 px-1.5 text-[10px] leading-none font-medium tabular-nums text-blue-600 dark:text-blue-400"
        >
          <Icon className="size-3" aria-hidden />
          {count}
        </span>
      ))}
    </span>
  );
}
