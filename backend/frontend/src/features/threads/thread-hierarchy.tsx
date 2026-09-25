import { useState } from "react";
import { ArrowUpLeft, ChevronRight, GitFork } from "lucide-react";
import type { Thread } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { threadActivity } from "./thread-activity";
import { ThreadActivityChips } from "./thread-activity-chips";
import { ThreadStatusIndicator } from "./thread-status-indicator";
import { subtreeThreads, threadTitle, treeIndent } from "./thread-tree";
import { subagentGroups } from "./subagent-summary";

export function ThreadHierarchy({
  thread,
  threads,
  onSelect,
}: {
  thread: Thread;
  threads: Thread[];
  onSelect: (id: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const subtree = subtreeThreads(threads, thread.thread_id);
  const parent = threads.find(
    (item) => item.thread_id === thread.parent_thread_id,
  );
  const byId = new Map(subtree.map((item) => [item.thread_id, item]));
  const counts = subagentGroups(subtree.slice(1));
  const depths = new Map<string, number>([[thread.thread_id, 0]]);
  const parents = new Set(
    subtree.slice(1).map((item) => item.parent_thread_id),
  );
  const hidden = new Set<string>();
  const rows = subtree.flatMap((item) => {
    const depth =
      item.thread_id === thread.thread_id
        ? 0
        : (depths.get(item.parent_thread_id) ?? 0) + 1;
    depths.set(item.thread_id, depth);
    if (
      item.thread_id !== thread.thread_id &&
      (collapsed.has(item.parent_thread_id) ||
        hidden.has(item.parent_thread_id))
    ) {
      hidden.add(item.thread_id);
      return [];
    }
    return [{ thread: item, depth }];
  });
  return (
    <section
      aria-label="Agent hierarchy"
      className="mt-5 space-y-3 border-b pb-5"
    >
      <header className="flex items-center justify-between gap-2">
        <h4 className="flex items-center gap-1.5 text-xs font-medium">
          <GitFork aria-hidden className="size-3.5 text-muted-foreground" />
          Agent hierarchy
        </h4>
        <span className="text-[11px] tabular-nums text-muted-foreground">
          {Math.max(0, subtree.length - 1)} subagents
        </span>
      </header>
      {parent ? (
        <button
          type="button"
          onClick={() => onSelect(parent.thread_id)}
          aria-label={`Open parent: ${threadTitle(parent)}`}
          title={threadTitle(parent)}
          className="flex max-w-full items-center gap-1.5 rounded text-xs text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
        >
          <ArrowUpLeft aria-hidden className="size-3.5 shrink-0" />
          <span className="shrink-0">Parent</span>
          <span className="truncate">{threadTitle(parent)}</span>
        </button>
      ) : null}
      {subtree.length > 1 ? (
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {counts.active.length ? (
            <span>{counts.active.length} active</span>
          ) : null}
          {counts.attention.length ? (
            <span className="text-amber-700 dark:text-amber-400">
              {counts.attention.length} need attention
            </span>
          ) : null}
          {counts.completed.length ? (
            <span>{counts.completed.length} finished</span>
          ) : null}
        </p>
      ) : null}
      <ul aria-label="Thread subtree" className="space-y-1">
        {rows.map(({ thread: item, depth }) => {
          const isCurrent = item.thread_id === thread.thread_id;
          const state = threadActivity(item);
          const hasChildren = parents.has(item.thread_id);
          const open = !collapsed.has(item.thread_id);
          const parentTitle = byId.get(item.parent_thread_id);
          return (
            <li key={item.thread_id} className={treeIndent[Math.min(depth, 6)]}>
              <div
                className={cn(
                  "relative flex items-start gap-1 rounded-lg py-1",
                  isCurrent ? "bg-muted/70" : "hover:bg-muted/40",
                  depth > 0 && "border-s border-border",
                )}
              >
                {hasChildren ? (
                  <button
                    type="button"
                    aria-label={`${open ? "Collapse" : "Expand"} ${threadTitle(item)}`}
                    aria-expanded={open}
                    className="mt-1.5 flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
                    onClick={() =>
                      setCollapsed((previous) => {
                        const next = new Set(previous);
                        if (open) next.add(item.thread_id);
                        else next.delete(item.thread_id);
                        return next;
                      })
                    }
                  >
                    <ChevronRight
                      aria-hidden
                      className={cn(
                        "size-3 transition-transform",
                        open && "rotate-90",
                      )}
                    />
                  </button>
                ) : (
                  <span aria-hidden className="w-5 shrink-0" />
                )}
                <button
                  type="button"
                  aria-current={isCurrent ? "true" : undefined}
                  title={`${threadTitle(item)}${parentTitle ? ` · Parent: ${threadTitle(parentTitle)}` : ""}`}
                  onClick={() => onSelect(item.thread_id)}
                  className="flex min-w-0 flex-1 items-start gap-2 rounded-md py-1.5 pe-2 text-start focus-visible:outline-2 focus-visible:outline-ring"
                >
                  <ThreadStatusIndicator thread={item} size="sm" />
                  <span className="min-w-0 flex-1 space-y-1.5">
                    <span className="line-clamp-2 break-words text-xs leading-4 font-medium">
                      {threadTitle(item)}
                    </span>
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span
                        className={cn(
                          "text-[10px]",
                          state.tone === "error"
                            ? "text-amber-700 dark:text-amber-400"
                            : "text-muted-foreground",
                        )}
                      >
                        {state.label}
                      </span>
                      {isCurrent ? (
                        <span className="text-[9px] text-muted-foreground">
                          This thread
                        </span>
                      ) : null}
                      <ThreadActivityChips thread={item} />
                    </span>
                  </span>
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
