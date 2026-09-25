import { useEffect, useId, useRef, useState } from "react";
import { ArrowUpRight, ChevronRight, GitFork } from "lucide-react";
import type { Thread } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import type { TaskConversationItem } from "./transcript-model";
import { taskArguments } from "./task-conversation-model";
import { TaskHandoff, TaskToolCard } from "./task-handoff";
import { ThreadStatusIndicator } from "./thread-status-indicator";
import { threadTitle } from "./thread-tree";

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});

export function TaskConversation({
  item,
  threads = [],
  onThread,
}: {
  item: TaskConversationItem;
  threads?: Thread[];
  onThread?: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const scrollRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const child = threads.find((thread) => thread.thread_id === item.threadId);
  const name = item.handle || (child ? threadTitle(child) : "subagent");
  const pending = item.delegation && !item.delegation.result;
  const failed = item.entries.some((entry) =>
    entry.kind === "task"
      ? entry.status === "rejected"
      : entry.result?.result_kind === "error" ||
        taskArguments(entry.result?.result).status === "rejected",
  );
  const title = failed
    ? "Task action failed"
    : pending
      ? item.delegation?.execution === "stopped"
        ? "Delegation stopped"
        : "Delegating task"
      : "Delegated task";
  useEffect(() => {
    const element = scrollRef.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [expanded, item.entries]);
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-border/70 bg-background/50 text-foreground">
      <div className="flex items-start gap-1 p-1">
        <button
          type="button"
          aria-label={`${expanded ? "Collapse" : "Expand"} conversation with ${name}`}
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded(!expanded)}
          className="flex min-w-0 flex-1 items-start gap-2.5 rounded-md px-2 py-2 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring"
        >
          {child ? (
            <ThreadStatusIndicator thread={child} size="sm" />
          ) : (
            <GitFork
              aria-hidden
              className="mt-0.5 size-4 shrink-0 text-muted-foreground"
            />
          )}
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              <span className={cn("font-medium", failed && "text-destructive")}>
                {title}
              </span>
              {item.handle ? (
                <span className="text-[10px] text-muted-foreground">
                  {item.handle}
                </span>
              ) : null}
              <span className="text-[10px] text-muted-foreground">
                {item.entries.length}{" "}
                {item.entries.length === 1 ? "entry" : "entries"}
              </span>
            </span>
            {item.objective ? (
              <span className="mt-1 line-clamp-2 whitespace-pre-wrap wrap-anywhere text-xs leading-5 text-muted-foreground">
                {item.objective}
              </span>
            ) : null}
          </span>
          <ChevronRight
            aria-hidden
            className={cn(
              "mt-0.5 size-3.5 shrink-0 text-muted-foreground transition-transform",
              expanded && "rotate-90",
            )}
          />
        </button>
        {item.threadId && onThread ? (
          <button
            type="button"
            aria-label={`Open subagent: ${child ? threadTitle(child) : name}`}
            onClick={() => onThread(item.threadId!)}
            className="mt-1 rounded p-2 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
          >
            <ArrowUpRight aria-hidden className="size-3.5" />
          </button>
        ) : null}
      </div>
      {expanded ? (
        <div
          ref={scrollRef}
          id={id}
          role="region"
          aria-label={`Conversation with ${name}`}
          tabIndex={0}
          className="max-h-96 overflow-y-auto overscroll-contain border-t border-border/70 bg-muted/10 p-3 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
          onScroll={(event) => {
            const element = event.currentTarget;
            following.current =
              element.scrollHeight - element.scrollTop - element.clientHeight <
              40;
          }}
        >
          <ol className="flex flex-col gap-4">
            {item.entries.map((entry) => {
              const parent = entry.kind === "tool";
              const date =
                entry.timestamp === undefined
                  ? null
                  : new Date(entry.timestamp * 1000);
              return (
                <li
                  key={entry.key}
                  className={cn(
                    "min-w-0 w-[92%]",
                    parent ? "self-end" : "self-start",
                  )}
                >
                  <div
                    className={cn(
                      "mb-1 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground",
                      parent && "justify-end",
                    )}
                  >
                    <span className="font-medium text-foreground">
                      {parent ? "Parent agent" : "Subagent"}
                    </span>
                    {date && Number.isFinite(date.getTime()) ? (
                      <time
                        dateTime={date.toISOString()}
                        title={date.toLocaleString()}
                      >
                        {timeFormat.format(date)}
                      </time>
                    ) : null}
                  </div>
                  <div
                    className={cn(
                      "rounded-lg border px-3 py-2.5",
                      parent
                        ? "border-border bg-muted/40"
                        : "border-border/70 bg-background",
                    )}
                  >
                    {entry.kind === "task" ? (
                      <TaskHandoff item={entry} nested />
                    ) : (
                      <TaskToolCard item={entry} nested />
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      ) : null}
    </div>
  );
}
