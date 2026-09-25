import { ChevronRight } from "lucide-react";
import type { Thread, ThreadDetail } from "@/lib/api-types";
import { ThreadHierarchy } from "./thread-hierarchy";
import { number } from "@/lib/format";
import {
  pendingPrompts,
  runningCommand,
  threadActivity,
  threadToolCounts,
} from "./thread-activity";
import { ThreadStatusIndicator } from "./thread-status-indicator";
import { ThreadStateSkeleton } from "./thread-skeletons";

function timestamp(seconds: number) {
  return new Date(seconds * 1_000).toLocaleString();
}

const scheduleNames: Record<string, string> = {
  wake: "Scheduled wake",
  "model-retry": "Model retry",
  quiescence: "Command shutdown deadline",
};

function countLabel(count: number, singular: string) {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

export function ThreadStatePanel({
  thread,
  error,
  threads,
  onSelect,
}: {
  thread?: ThreadDetail;
  error?: string;
  threads?: Thread[];
  onSelect?: (id: string) => void;
}) {
  if (!thread && error)
    return (
      <p role="alert" className="p-5 text-sm text-destructive">
        {error}
      </p>
    );
  if (!thread) return <ThreadStateSkeleton />;
  const activity = thread.activity;
  const state = threadActivity(thread);
  const tools = threadToolCounts(thread);
  const prompts = pendingPrompts(thread);
  const queued = prompts + tools.queued;
  return (
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5 text-sm [scrollbar-width:thin]">
      <div role="status" className="flex items-center gap-2 font-medium">
        <ThreadStatusIndicator thread={thread} />
        {state.label}
      </div>
      {error ? (
        <p role="alert" className="mt-4 text-xs text-destructive">
          State may be out of date: {error}
        </p>
      ) : null}
      {threads && onSelect ? (
        <ThreadHierarchy
          key={thread.thread_id}
          thread={thread}
          threads={threads}
          onSelect={onSelect}
        />
      ) : null}
      {activity ? (
        <div className="mt-5 space-y-5">
          {activity.running_tool &&
          thread.live &&
          activity.phase !== "terminal" ? (
            <section aria-label="Current command" className="space-y-2">
              <h4 className="text-xs font-medium text-muted-foreground">
                Current command
              </h4>
              <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 px-3 py-2.5 font-mono text-xs">
                {runningCommand(thread)}
              </pre>
            </section>
          ) : null}
          {queued > 0 ? (
            <section
              aria-label="Work queue"
              className="flex items-center justify-between gap-4 border-y py-3"
            >
              <div className="min-w-0 space-y-0.5">
                <h4 className="text-xs font-medium">Queue</h4>
                <p className="truncate text-xs text-muted-foreground">
                  {[
                    prompts > 0 ? countLabel(prompts, "prompt") : null,
                    tools.queued > 0 ? countLabel(tools.queued, "tool") : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
              <span className="shrink-0 text-lg font-medium tabular-nums">
                {queued}
              </span>
            </section>
          ) : null}
          {activity.consolidating.length ? (
            <section className="space-y-2" aria-label="Curation">
              <h4 className="text-xs font-medium">Curating changes</h4>
              <p className="text-xs text-muted-foreground">
                {activity.consolidating.join(" → ")}
              </p>
            </section>
          ) : null}
          {thread.error ? (
            <p className="rounded-md bg-destructive/5 px-3 py-2.5 text-xs text-destructive">
              {thread.error}
            </p>
          ) : null}
          {activity.schedules.length ? (
            <section aria-label="Scheduled work" className="space-y-2">
              <h4 className="text-xs font-medium">Scheduled</h4>
              <ul className="divide-y text-xs">
                {activity.schedules.map((schedule) => (
                  <li
                    key={schedule.key}
                    className="space-y-0.5 py-2 first:pt-0"
                  >
                    <p>
                      {thread.signals?.find(
                        (signal) => `signal:${signal.id}` === schedule.key,
                      )?.note ??
                        scheduleNames[schedule.key] ??
                        (schedule.key.startsWith("signal:")
                          ? "Scheduled signal"
                          : schedule.key)}
                    </p>
                    <p className="text-muted-foreground">
                      <time
                        dateTime={new Date(
                          schedule.due_at * 1_000,
                        ).toISOString()}
                      >
                        {timestamp(schedule.due_at)}
                      </time>
                    </p>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          <details className="group border-t pt-4 text-xs">
            <summary className="flex cursor-pointer list-none items-center gap-1.5 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
              <ChevronRight
                aria-hidden
                className="size-3.5 transition-transform group-open:rotate-90"
              />
              Details
            </summary>
            <dl className="mt-4 space-y-3">
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Runtime inbox</dt>
                <dd className="tabular-nums">{activity.mailbox_depth}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Completed turns</dt>
                <dd className="tabular-nums">{number(thread.turns)}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Tokens used</dt>
                <dd className="tabular-nums">
                  {number(thread.input_tokens + thread.output_tokens)}
                </dd>
              </div>
              {thread.compactions > 0 ? (
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Compactions</dt>
                  <dd className="tabular-nums">{number(thread.compactions)}</dd>
                </div>
              ) : null}
              <div className="flex justify-between gap-3">
                <dt className="text-muted-foreground">Updated</dt>
                <dd className="text-right">
                  <time
                    dateTime={new Date(
                      activity.updated_at * 1_000,
                    ).toISOString()}
                  >
                    {timestamp(activity.updated_at)}
                  </time>
                </dd>
              </div>
            </dl>
          </details>
        </div>
      ) : (
        <p className="mt-4 text-xs text-muted-foreground">
          Runtime details are unavailable.
        </p>
      )}
    </div>
  );
}
