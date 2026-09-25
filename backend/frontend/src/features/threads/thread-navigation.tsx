import { useDeferredValue, useState } from "react";
import { Plus, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChatOriginIcon } from "@/components/chat-origin-icon";
import { chatAttentionLabel } from "@/lib/chat-sidebar";
import type { Thread } from "@/lib/api-types";
import { threadActivity } from "./thread-activity";
import { ThreadActivityChips } from "./thread-activity-chips";
import { SubagentSummary } from "./subagent-summary";
import { ThreadStatusIndicator } from "./thread-status-indicator";
import { ThreadStatusBadge, threadStatusBadge } from "./thread-status-badge";
import { ThreadTreeGuides, threadTreeBranches } from "./thread-tree-guides";

import {
  logicalThreads,
  subtreeThreads,
  threadTitle,
  treeIndent,
  type TreeThread,
} from "./thread-tree";

const noOptimisticWakes = new Set<string>();

export function ThreadNavigation({
  threads,
  selected,
  optimisticallyAwake = noOptimisticWakes,
  onSelect,
}: {
  threads: Thread[];
  selected: string | null;
  optimisticallyAwake?: ReadonlySet<string>;
  onSelect: (id: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Map<string, boolean>>(
    () => new Map(),
  );
  function toggle(id: string, open: boolean) {
    setExpanded((current) => new Map(current).set(id, !open));
  }
  const search = useDeferredValue(filter.trim().toLowerCase());
  const logical = logicalThreads(threads, selected, search);
  const recent: TreeThread[] = [];
  let hiddenBelow: number | null = null;
  for (const item of logical) {
    if (hiddenBelow !== null && item.depth > hiddenBelow) continue;
    hiddenBelow = null;
    recent.push(item);
    const open =
      search || (expanded.get(item.thread.thread_id) ?? item.forceExpanded);
    if (item.hasChildren && !open) hiddenBelow = item.depth;
  }
  const branches = threadTreeBranches(recent);
  return (
    <section
      className="flex min-h-0 min-w-0 flex-1 flex-col"
      aria-label="Threads"
    >
      <div className="flex shrink-0 items-center justify-between px-5 pb-2">
        <h2 className="text-xs font-medium text-muted-foreground">Threads</h2>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="New thread"
          title="New thread"
          onClick={() => onSelect("")}
        >
          <Plus />
        </Button>
      </div>
      <label className="mx-4 mb-2 flex shrink-0 items-center gap-2 rounded-lg bg-muted/70 px-2.5">
        <Search className="size-3.5 text-muted-foreground" />
        <input
          aria-label="Search threads"
          placeholder="Search threads"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          className="h-8 min-w-0 flex-1 bg-transparent text-xs outline-none"
        />
      </label>
      <div
        className="scrollbar-thin flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto overscroll-contain px-3 pb-4"
        role="tree"
        aria-label="Thread list"
        tabIndex={0}
        onKeyDown={(event) => {
          const item = (event.target as HTMLElement).closest<HTMLElement>(
            '[role="treeitem"]',
          );
          if (
            !item ||
            (event.target !== item &&
              (event.key === "Enter" || event.key === " "))
          )
            return;
          const items = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              '[role="treeitem"]',
            ),
          );
          const index = items.indexOf(item);
          const level = Number(item.getAttribute("aria-level"));
          if (event.key === "ArrowDown" && index < items.length - 1) {
            items[index + 1].focus();
          } else if (event.key === "ArrowUp" && index > 0) {
            items[index - 1].focus();
          } else if (event.key === "ArrowRight") {
            if (item.getAttribute("aria-expanded") === "false") {
              setExpanded((current) =>
                new Map(current).set(item.dataset.threadId!, true),
              );
            } else if (
              index < items.length - 1 &&
              Number(items[index + 1].getAttribute("aria-level")) > level
            ) {
              items[index + 1].focus();
            }
          } else if (event.key === "ArrowLeft") {
            if (item.getAttribute("aria-expanded") === "true") {
              setExpanded((current) => {
                const next = new Map(current);
                next.set(item.dataset.threadId!, false);
                return next;
              });
            } else {
              for (let parent = index - 1; parent >= 0; parent -= 1) {
                if (Number(items[parent].getAttribute("aria-level")) < level) {
                  items[parent].focus();
                  break;
                }
              }
            }
          } else if (event.key === "Enter" || event.key === " ") {
            onSelect(item.dataset.threadId!);
          } else {
            return;
          }
          event.preventDefault();
        }}
      >
        {recent.map(({ thread, depth, hasChildren, forceExpanded }, index) => {
          const title = threadTitle(thread);
          const descendants = hasChildren
            ? subtreeThreads(threads, thread.thread_id).slice(1)
            : [];
          const optimisticAwake = optimisticallyAwake.has(thread.thread_id);
          const sandboxActive =
            optimisticAwake || (thread.activity?.sandbox_active ?? thread.live);
          const sleeping =
            thread.live &&
            !thread.archived &&
            thread.activity?.phase !== "terminal" &&
            !sandboxActive;
          const activity = threadActivity(thread);
          const badge = threadStatusBadge(thread);
          const statusLabel = badge?.label ?? activity.label;
          const open = Boolean(
            hasChildren &&
            (search || (expanded.get(thread.thread_id) ?? forceExpanded)),
          );
          return (
            <div
              key={thread.thread_id}
              className={`relative w-full min-w-0 ${treeIndent[Math.min(depth, 6)]}`}
              data-thread-depth={depth}
              data-thread-id={thread.thread_id}
              role="treeitem"
              aria-level={depth + 1}
              aria-expanded={hasChildren ? open : undefined}
              tabIndex={
                thread.thread_id === selected ||
                (!selected && recent[0]?.thread.thread_id === thread.thread_id)
                  ? 0
                  : -1
              }
            >
              <ThreadTreeGuides branches={branches[index]} />
              <div className="relative h-16">
                <Button
                  className={
                    "h-16 w-full min-w-0 justify-start gap-3 overflow-hidden rounded-xl py-3 text-left " +
                    "px-3" +
                    (sleeping ? " opacity-85" : "")
                  }
                  variant={
                    thread.thread_id === selected ? "secondary" : "ghost"
                  }
                  tabIndex={-1}
                  aria-current={
                    thread.thread_id === selected ? "true" : undefined
                  }
                  data-sandbox-active={sandboxActive}
                  data-thread-state={
                    sleeping ? "sleeping" : thread.live ? "awake" : "finished"
                  }
                  title={title}
                  onClick={() => onSelect(thread.thread_id)}
                >
                  <span className="relative inline-flex shrink-0">
                    <ThreadStatusIndicator
                      thread={thread}
                      forceAwake={optimisticAwake}
                    />
                    {badge ? <ThreadStatusBadge badge={badge} /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex min-w-0 items-center gap-1.5">
                      {thread.attention ? (
                        <span
                          className={`size-2 shrink-0 rounded-full ${thread.attention === "result_available" ? "bg-status-green-700" : "bg-status-amber-700"}`}
                          title={chatAttentionLabel({ attention_reason: thread.attention }) ?? undefined}
                          aria-label={chatAttentionLabel({ attention_reason: thread.attention }) ?? undefined}
                        />
                      ) : null}
                      {thread.trigger?.startsWith("slack:") ||
                      thread.trigger?.startsWith("github:") ? (
                        <span className="text-muted-foreground">
                          <ChatOriginIcon trigger={thread.trigger} />
                        </span>
                      ) : null}
                      <strong className="min-w-0 flex-1 truncate text-sm font-medium">
                        {title}
                      </strong>
                      <ThreadActivityChips thread={thread} />
                    </span>
                    <span
                      className="mt-1 flex h-4 min-w-0 items-center gap-1.5 text-xs font-normal text-muted-foreground"
                      data-thread-meta
                    >
                      {!hasChildren ? (
                        <span className="min-w-0 truncate whitespace-nowrap">
                          {statusLabel}
                        </span>
                      ) : null}
                    </span>
                  </span>
                </Button>
                {hasChildren ? (
                  <div className="absolute start-13 end-3 bottom-2">
                    <SubagentSummary
                      threads={descendants}
                      expanded={open}
                      onToggle={() => toggle(thread.thread_id, open)}
                      title={title}
                      optimisticallyAwake={optimisticallyAwake}
                    />
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
        {!recent.length ? (
          <p className="px-2 py-4 text-xs text-muted-foreground">
            {search ? "No matching threads." : "No conversations yet."}
          </p>
        ) : null}
      </div>
    </section>
  );
}
