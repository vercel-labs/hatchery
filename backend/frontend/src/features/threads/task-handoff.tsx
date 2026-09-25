import {
  ArrowUpRight,
  Check,
  ChevronRight,
  CircleAlert,
  ClipboardCheck,
  GitFork,
  MessageCircle,
  Send,
} from "lucide-react";
import type { Thread } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import type { Item, ToolItem } from "./transcript-model";
import { ThreadStatusIndicator } from "./thread-status-indicator";
import { threadTitle } from "./thread-tree";
import { taskArguments, taskField } from "./task-conversation-model";
import { MarkdownText } from "./markdown-text";
export { taskArguments } from "./task-conversation-model";

function taskSection(text: string, field: string): string {
  const headings =
    "Task|Task ID|Reply target|Objective|Status|Message|Summary|Result|Deliverables|Proposals|Child roster";
  return (
    text
      .match(
        new RegExp(
          `(?:^|\\n)${field}:[ \\t]*([\\s\\S]*?)(?=\\n(?:${headings}):|$)`,
        ),
      )?.[1]
      ?.trim() ?? ""
  );
}

export function TaskHandoff({
  item,
  threads = [],
  onThread,
  nested = false,
}: {
  item: Extract<Item, { kind: "task" }>;
  threads?: Thread[];
  onThread?: (id: string) => void;
  nested?: boolean;
}) {
  const child = threads.find((thread) => thread.thread_id === item.threadId);
  const status = item.status || taskField(item.text, "Status");
  const completion = item.event === "completion" || status === "completed";
  const attention = status === "rejected";
  const Icon = completion ? Check : attention ? CircleAlert : MessageCircle;
  const objective = taskField(item.text, "Objective");
  const handle =
    item.handle ||
    taskField(item.text, "Task") ||
    taskField(item.text, "Task ID");
  const label = completion
    ? "Subagent completed"
    : status === "rejected"
      ? "Task action rejected"
      : "Message from subagent";
  const summary =
    taskSection(item.text, "Summary") || taskField(item.text, "Reason");
  const message =
    item.event === "message"
      ? (item.text.match(/(?:^|\n)Message:[ \t]*(?:\r?\n)?([\s\S]*)$/)?.[1] ??
        "")
      : taskSection(item.text, "Message");
  const result = taskSection(item.text, "Result");
  return (
    <div
      className={cn(
        nested ? "min-w-0" : "min-w-0 rounded-xl border px-3.5 py-3",
        attention
          ? "border-amber-500/25 bg-amber-500/5"
          : !nested && "border-border/70 bg-muted/20",
      )}
    >
      {!nested || completion || attention ? (
        <div className="flex items-center gap-2">
          <Icon
            aria-hidden
            className={cn(
              "size-3.5 shrink-0",
              completion
                ? "text-emerald-600 dark:text-emerald-400"
                : attention
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-blue-600 dark:text-blue-400",
            )}
          />
          <span className="text-xs font-medium">{label}</span>
          {!nested && handle && /^task-\d+$/.test(handle) ? (
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {handle}
            </span>
          ) : null}
          {!nested && child && onThread ? (
            <button
              type="button"
              onClick={() => onThread(child.thread_id)}
              aria-label={`Open subagent: ${threadTitle(child)}`}
              title={threadTitle(child)}
              className="ms-auto rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
            >
              <ArrowUpRight aria-hidden className="size-3.5" />
            </button>
          ) : null}
        </div>
      ) : null}
      {!nested && objective ? (
        <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">
          {objective}
        </p>
      ) : null}
      {message || summary ? (
        nested ? (
          <MarkdownText text={message || summary} compact />
        ) : (
          <p className="whitespace-pre-wrap wrap-anywhere text-xs leading-5">
            {message || summary}
          </p>
        )
      ) : nested && !completion && !attention ? (
        <MarkdownText text={item.text} compact />
      ) : null}
      {completion || attention ? (
        <details className="group/handoff mt-2.5">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
            <ChevronRight
              aria-hidden
              className="size-3 transition-transform group-open/handoff:rotate-90"
            />
            {completion ? "View result & artifacts" : "View rejection details"}
          </summary>
          <div className="mt-2 max-h-96 space-y-3 overflow-auto border-t pt-3 text-xs leading-5">
            {objective ? (
              <p className="whitespace-pre-wrap text-muted-foreground">
                {objective}
              </p>
            ) : null}
            {completion && summary ? (
              <p className="whitespace-pre-wrap wrap-anywhere">{summary}</p>
            ) : null}
            {result ? (
              <div>
                <h5 className="mb-1 font-medium text-muted-foreground">
                  Result
                </h5>
                <p className="whitespace-pre-wrap wrap-anywhere">{result}</p>
              </div>
            ) : null}
            {["Deliverables", "Proposals"].map((field) => {
              const value = taskSection(item.text, field);
              return value ? (
                <div key={field}>
                  <h5 className="mb-1 font-medium text-muted-foreground">
                    {field}
                  </h5>
                  <p className="whitespace-pre-wrap wrap-anywhere">{value}</p>
                </div>
              ) : null;
            })}
            <details>
              <summary className="cursor-pointer text-[11px] text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring">
                Raw message
              </summary>
              <p className="mt-2 whitespace-pre-wrap wrap-anywhere text-muted-foreground">
                {item.text}
              </p>
            </details>
          </div>
        </details>
      ) : null}
    </div>
  );
}

export function TaskToolCard({
  item,
  threads = [],
  onThread,
  nested = false,
}: {
  item: ToolItem;
  threads?: Thread[];
  onThread?: (id: string) => void;
  nested?: boolean;
}) {
  const args = taskArguments(item.call?.tool_args);
  const result = taskArguments(item.result?.result);
  const handle = String(result.task_id ?? args.task_id ?? "");
  const targetsChild = [
    "delegate",
    "message_task",
    "cancel_task",
    "archive_task",
    "review_proposal",
  ].includes(item.name);
  const child =
    handle && targetsChild
      ? threads.find(
          (thread) =>
            thread.task_handle === handle || thread.task_id === handle,
        )
      : undefined;
  const failed =
    item.result?.result_kind === "error" || result.status === "rejected";
  const pending = !item.result;
  const stopped = pending && item.execution === "stopped";
  const labels: Record<string, [string, string]> = {
    delegate: ["Delegating task", "Delegated task"],
    message_task: [
      "Sending message",
      result.status === "queued" ? "Message queued" : "Message accepted",
    ],
    complete: ["Completing task", "Completion handoff started"],
    task_roster: ["Checking subagents", "Checked subagents"],
    cancel_task: ["Requesting cancellation", "Cancellation requested"],
    archive_task: ["Requesting archive", "Archive requested"],
    review_proposal: ["Reviewing proposal", "Reviewed proposal"],
  };
  const pair = labels[item.name] ?? [item.name, item.name];
  const title = failed
    ? `${pair[0]} failed`
    : stopped
      ? "Stopped"
      : pair[pending ? 0 : 1];
  const text = String(
    args.objective ??
      args.text ??
      args.summary ??
      args.reason ??
      args.comment ??
      "",
  );
  const Icon =
    item.name === "delegate"
      ? GitFork
      : item.name === "message_task"
        ? Send
        : ClipboardCheck;
  return (
    <div
      role="group"
      aria-label={`${item.name} tool call`}
      className={
        nested
          ? "min-w-0"
          : "min-w-0 rounded-lg border border-border/70 bg-background/50 px-3 py-2.5"
      }
    >
      <div className="flex min-w-0 items-start gap-2.5">
        {nested ? null : child ? (
          <ThreadStatusIndicator thread={child} size="sm" />
        ) : (
          <Icon
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className={cn("font-medium", failed && "text-destructive")}>
              {title}
            </span>
            {!nested && /^task-\d+$/.test(handle) ? (
              <span className="text-[10px] text-muted-foreground">
                {handle}
              </span>
            ) : null}
          </div>
          {text ? (
            nested ? (
              <MarkdownText text={text} compact />
            ) : (
              <p className="mt-1 line-clamp-2 whitespace-pre-wrap wrap-anywhere text-xs leading-5 text-muted-foreground">
                {text}
              </p>
            )
          ) : null}
        </div>
        {!nested && child && onThread ? (
          <button
            type="button"
            onClick={() => onThread(child.thread_id)}
            aria-label={`Open subagent: ${threadTitle(child)}`}
            className="rounded p-1 text-muted-foreground hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
          >
            <ArrowUpRight aria-hidden className="size-3.5" />
          </button>
        ) : null}
      </div>
      <details className="group/task mt-2 text-[11px] text-muted-foreground">
        <summary className="flex w-fit cursor-pointer list-none items-center gap-1 focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
          <ChevronRight
            aria-hidden
            className="size-3 transition-transform group-open/task:rotate-90"
          />
          Tool details
        </summary>
        <div className="mt-2 max-h-72 space-y-2 overflow-auto border-t pt-2 font-mono leading-5">
          {item.call ? (
            <pre
              aria-label="Tool input"
              className="whitespace-pre-wrap wrap-anywhere"
            >
              {Object.keys(args).length ||
              typeof item.call.tool_args !== "string"
                ? JSON.stringify(args, null, 2)
                : item.call.tool_args}
            </pre>
          ) : null}
          {item.result ? (
            <pre
              aria-label="Tool output"
              className="whitespace-pre-wrap wrap-anywhere"
            >
              {typeof item.result.result === "string"
                ? item.result.result
                : JSON.stringify(item.result.result, null, 2)}
            </pre>
          ) : (
            <p>
              {stopped
                ? "Stopped before a result was recorded."
                : "Awaiting result"}
            </p>
          )}
        </div>
      </details>
    </div>
  );
}

export function subagentWorkLabel(
  items: Item[],
  working: boolean,
): string | undefined {
  const tools = items.flatMap((item): ToolItem[] =>
    item.kind === "tool"
      ? [item]
      : item.kind === "task-conversation"
        ? item.delegation
          ? [item.delegation]
          : item.entries.filter((entry) => entry.kind === "tool")
        : [],
  );
  const taskTools = tools.filter((item) =>
    ["delegate", "message_task", "complete", "task_roster"].includes(item.name),
  );
  if (
    taskTools.some(
      (item) =>
        item.result?.result_kind === "error" ||
        taskArguments(item.result?.result).status === "rejected",
    )
  )
    return "Subagent action failed";
  if (taskTools.some((item) => !item.result && item.execution === "stopped"))
    return "Subagent action stopped";
  const delegated = tools.filter((item) => item.name === "delegate");
  const messages = tools.filter((item) => item.name === "message_task");
  if (delegated.length)
    return `${working ? "Delegating" : "Delegated"} ${delegated.length} ${delegated.length === 1 ? "task" : "tasks"}`;
  if (messages.length)
    return working ? "Messaging subagents" : "Requested subagent follow-up";
  if (tools.some((item) => item.name === "complete"))
    return working ? "Completing task" : "Completed task";
  if (tools.some((item) => item.name === "task_roster"))
    return working ? "Checking subagents" : "Checked subagents";
  return undefined;
}
