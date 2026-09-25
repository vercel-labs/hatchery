import { ChevronRight } from "lucide-react";
import type { Thread } from "@/lib/api-types";
import { threadActivity } from "./thread-activity";
import { ThreadStatusIndicator } from "./thread-status-indicator";
import { cn } from "@/lib/utils";

export function subagentGroups(threads: Thread[]) {
  const active: Thread[] = [],
    completed: Thread[] = [],
    attention: Thread[] = [];
  for (const thread of threads) {
    if (thread.archived) continue;
    const state = threadActivity(thread);
    if (state.tone === "error") attention.push(thread);
    else if (
      state.working ||
      (!["completed", "cancelled"].includes(thread.task_status) && thread.live)
    )
      active.push(thread);
    else completed.push(thread);
  }
  // Keep actively executing descendants ahead of idle workers.
  active.sort(
    (left, right) =>
      Number(threadActivity(right).working) -
      Number(threadActivity(left).working),
  );
  return { active, completed, attention };
}

function BotStack({
  thread,
  count = 1,
  forceAwake = false,
}: {
  thread: Thread;
  count?: number;
  forceAwake?: boolean;
}) {
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1"
      data-bot-stack={count}
    >
      <span className="inline-flex items-center">
        <ThreadStatusIndicator
          thread={thread}
          size="xs"
          forceAwake={forceAwake}
        />
        {count > 1 ? (
          <span aria-hidden className="inline-flex items-center">
            {[
              "border-muted-foreground/40",
              "border-muted-foreground/30",
              "border-muted-foreground/20",
            ].map((border) => (
              // Expose only the trailing edge, keeping outlines out of the translucent bot.
              <span
                key={border}
                className="relative h-4 w-[2.5px] overflow-hidden"
              >
                <span
                  className={cn(
                    "absolute inset-y-0 end-0 size-4 rounded-[5px] border",
                    border,
                  )}
                />
              </span>
            ))}
          </span>
        ) : null}
      </span>
      {count > 1 ? (
        <span className="text-[10px] leading-4 font-medium tabular-nums">
          {count}
        </span>
      ) : null}
    </span>
  );
}

export function SubagentSummary({
  threads,
  expanded,
  onToggle,
  title,
  optimisticallyAwake,
}: {
  threads: Thread[];
  expanded: boolean;
  onToggle: () => void;
  title: string;
  optimisticallyAwake: ReadonlySet<string>;
}) {
  const { active, completed, attention } = subagentGroups(threads);
  const total = active.length + completed.length + attention.length;
  const counts = total
    ? [
        active.length ? `${active.length} active` : "",
        attention.length ? `${attention.length} need attention` : "",
        completed.length ? `${completed.length} finished` : "",
      ]
        .filter(Boolean)
        .join(", ")
    : "Archived subagents";
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      aria-label={`${expanded ? "Collapse" : "Expand"} subagents for ${title}`}
      title={`${counts} · Click to ${expanded ? "collapse" : "expand"}`}
      className="flex h-6 max-w-full items-center gap-1.5 rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
    >
      <span className="sr-only">{counts}</span>
      <span
        className="flex min-w-0 items-center gap-1 overflow-x-auto py-1 [scrollbar-width:none]"
        aria-hidden
      >
        {!total ? (
          <span className="truncate text-xs">Archived subagents</span>
        ) : null}
        {active.slice(0, 4).map((thread, index) => (
          <BotStack
            key={thread.thread_id}
            thread={thread}
            count={index === 3 ? active.length - 3 : 1}
            forceAwake={optimisticallyAwake.has(thread.thread_id)}
          />
        ))}
        {attention.length ? (
          <span className="flex shrink-0 items-center gap-1 border-s border-border ps-1.5 first:border-0 first:ps-0">
            <BotStack thread={attention[0]} count={attention.length} />
          </span>
        ) : null}
        {completed.length ? (
          <span className="flex shrink-0 items-center gap-1 border-s border-border ps-1.5 first:border-0 first:ps-0">
            <BotStack thread={completed[0]} count={completed.length} />
          </span>
        ) : null}
      </span>
      <ChevronRight
        aria-hidden
        className={cn(
          "size-3 shrink-0 transition-transform",
          expanded && "rotate-90",
        )}
      />
    </button>
  );
}
