import { memo, useMemo } from "react";
import {
  ArrowUpLeft,
  Bot,
  CalendarClock,
  ChevronRight,
  Webhook,
} from "lucide-react";
import type { Message, Thread, ToolProgress } from "@/lib/api-types";
import { groupMessages, type Group, type Item } from "./transcript-model";
import { ToolCard } from "./tool-call";
import { TaskHandoff, TaskToolCard, subagentWorkLabel } from "./task-handoff";
import { threadTitle } from "./thread-tree";
import { collectTaskConversations } from "./task-conversation-model";
import { TaskConversation } from "./task-conversation";
import { MarkdownText } from "./markdown-text";
import { ParentMessage } from "./parent-message";
import { HatcheryToolCard } from "./hatchery-tool";

const taskTools = new Set([
  "delegate",
  "message_task",
  "complete",
  "task_roster",
  "cancel_task",
  "archive_task",
  "review_proposal",
]);

// Thread tools render as console cards; Hatchery's own tools (fx subagents,
// extra sandboxes) keep Hatchery's tool part.
const consoleTools = new Set([
  ...taskTools,
  "bash",
  "skill_view",
  "signal",
  "schedule",
  "cancel",
  "open_repository",
  "message_parent",
  "secret_request",
]);

type ThreadContext = {
  threads?: Thread[];
  parentThread?: Thread;
  parentThreadId?: string;
  onThread?: (id: string) => void;
};

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});
const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});
const activeStatusClassName = "thinking-shimmer text-[11px] font-medium";

const emptyReply: Group = {
  key: "live",
  role: "assistant",
  activity: [],
  items: [],
};

function apiPromptPresentation(text: string): {
  summary: string;
  body: string;
} {
  const jsonStart = [text.indexOf("{"), text.indexOf("[")]
    .filter((index) => index >= 0)
    .sort((left, right) => left - right)[0];
  if (jsonStart !== undefined) {
    const prefix = text.slice(0, jsonStart).trim().replace(/:\s*$/, "");
    try {
      const json = JSON.stringify(JSON.parse(text.slice(jsonStart)), null, 2);
      return {
        summary: prefix || "Received structured input",
        body: prefix ? `${prefix}:\n${json}` : json,
      };
    } catch {
      // Handler prompts are free-form text and do not always contain JSON.
    }
  }
  const summary = text
    .split("\n")
    .find((line) => line.trim())
    ?.trim();
  return { summary: summary || "Received input", body: text };
}

function ApiCard({ item }: { item: Extract<Item, { kind: "api" }> }) {
  const { summary, body } = apiPromptPresentation(item.text);
  const scheduled = item.source === "schedule";
  const label = scheduled ? "Schedule signal" : "API signal";
  const Icon = scheduled ? CalendarClock : Webhook;
  return (
    <details
      aria-label={label}
      className="group/api min-w-0 overflow-hidden rounded-md border border-blue-500/20 bg-blue-500/4 text-xs"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-2 hover:bg-blue-500/6 focus-visible:bg-blue-500/8 focus-visible:outline-none [&::-webkit-details-marker]:hidden">
        <Icon
          aria-hidden
          className="size-4 shrink-0 text-blue-500"
          strokeWidth={1.75}
        />
        <span className="shrink-0 font-medium text-blue-600 dark:text-blue-400">
          {label}
        </span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          {summary}
        </span>
        <ChevronRight
          aria-hidden
          className="size-3.5 shrink-0 text-muted-foreground transition-transform group-open/api:rotate-90"
          strokeWidth={1.75}
        />
      </summary>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap wrap-anywhere border-t border-blue-500/15 px-2.5 py-2.5 font-mono leading-5 text-muted-foreground">
        {body}
      </pre>
    </details>
  );
}

function displayTimestamp(group: Group): number | undefined {
  return group.role === "assistant"
    ? (group.completedAt ?? group.timestamp)
    : group.timestamp;
}

function formatDuration(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 1) return "<1s";
  if (rounded < 60) return `${rounded}s`;
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

const workDisclosureIcon = (
  <svg
    aria-hidden
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    className="size-3 transition-transform group-open/work:rotate-90"
  >
    <path d="m6 3.5 4.5 4.5L6 12.5" />
  </svg>
);

// Saved items keep their component and DOM identity while live text changes.
const GroupItem = memo(function GroupItem({
  item,
  owner,
  streaming = false,
  markdown = false,
  threads,
  onThread,
}: {
  item: Item;
  owner?: string;
  streaming?: boolean;
  markdown?: boolean;
} & ThreadContext) {
  if (item.kind === "task-conversation")
    return (
      <TaskConversation item={item} threads={threads} onThread={onThread} />
    );
  if (item.kind === "task")
    return <TaskHandoff item={item} threads={threads} onThread={onThread} />;
  if (item.kind === "tool" && item.name === "message_parent")
    return <ParentMessage item={item} />;
  if (item.kind === "tool" && taskTools.has(item.name))
    return <TaskToolCard item={item} threads={threads} onThread={onThread} />;
  if (item.kind === "tool" && !consoleTools.has(item.name))
    return <HatcheryToolCard item={item} />;
  if (item.kind === "tool")
    return <ToolCard item={item} owner={owner} />;
  if (item.kind === "api") return <ApiCard item={item} />;
  if (item.kind === "text" && markdown)
    return (
      <MarkdownText
        text={item.text}
        streaming={streaming || item.streaming}
        messageText
      />
    );
  return (
    <p
      aria-label={streaming ? "Streaming response" : undefined}
      className={
        item.kind === "text"
          ? "whitespace-pre-wrap wrap-anywhere text-sm leading-7"
          : "whitespace-pre-wrap wrap-anywhere text-xs leading-5 text-muted-foreground"
      }
    >
      {item.text}
    </p>
  );
});

const MessageGroup = memo(function MessageGroup({
  group,
  active = false,
  live = "",
  liveKey = "live:text",
  owner,
  showDate = false,
  thinkingLabel = "Thinking",
  threads,
  parentThread,
  parentThreadId,
  onThread,
}: {
  group: Group;
  active?: boolean;
  live?: string;
  liveKey?: string;
  owner?: string;
  showDate?: boolean;
  thinkingLabel?: string;
} & ThreadContext) {
  const timestamp = displayTimestamp(group);
  const date = timestamp !== undefined ? new Date(timestamp * 1000) : null;
  const validDate = date && Number.isFinite(date.getTime()) ? date : null;
  const handoffs = group.items
    .filter((item) => item.kind === "task")
    .map((item) => (
      <GroupItem
        key={item.key}
        item={item}
        threads={threads}
        onThread={onThread}
      />
    ));
  const items = group.items
    .filter((item) => item.kind !== "task")
    .map((item) => (
      <GroupItem
        key={item.key}
        item={item}
        owner={owner}
        markdown={group.role === "assistant"}
        threads={threads}
        onThread={onThread}
      />
    ));
  const hasPendingTool = group.activity.some(
    (item) =>
      item.kind === "tool" && !item.result && item.execution !== "stopped",
  );
  const working = active || Boolean(live) || hasPendingTool;
  const thinking =
    group.role === "assistant" && working && !group.activity.length;
  const showTimestamp =
    validDate && !group.pending && !(group.role === "assistant" && working);
  const duration =
    group.startedAt !== undefined && group.completedAt !== undefined
      ? group.completedAt - group.startedAt
      : undefined;
  const workLabel =
    subagentWorkLabel(group.activity, working) ??
    (working
      ? "Working"
      : duration !== undefined
        ? `Worked for ${formatDuration(duration)}`
        : "Worked");
  const activity = group.activity.length ? (
    <details
      aria-label="Agent activity"
      className="group/work min-w-0 text-muted-foreground"
    >
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1 text-xs hover:text-foreground focus-visible:text-foreground focus-visible:underline focus-visible:underline-offset-4 focus-visible:outline-none [&::-webkit-details-marker]:hidden">
        <span className={working ? activeStatusClassName : undefined}>
          {workLabel}
        </span>
        {workDisclosureIcon}
      </summary>
      <div className="mt-2.5 space-y-2.5">
        {group.activity.map((item) => (
          <GroupItem
            key={item.key}
            item={item}
            owner={owner}
            markdown={group.role === "assistant"}
            threads={threads}
            onThread={onThread}
          />
        ))}
      </div>
    </details>
  ) : null;
  if (live) {
    items.push(
      <GroupItem
        key={liveKey}
        item={{ kind: "text", key: liveKey, text: live }}
        owner={owner}
        streaming
        markdown
      />,
    );
  }
  return (
    <article
      data-message-source={group.role}
      className={
        group.role === "user"
          ? "relative flex min-w-0 max-w-[90%] self-end flex-col items-end"
          : group.role === "parent"
            ? "relative flex min-w-0 max-w-[90%] self-start flex-col items-start"
            : "relative min-w-0 space-y-3"
      }
    >
      <div
        className={`mb-1.5 flex items-center gap-2 text-[11px] text-muted-foreground ${group.role === "user" ? "justify-end pr-1" : ""}`}
      >
        {group.role === "parent" ? (
          <>
            <span
              aria-hidden
              className="inline-flex size-4 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground"
            >
              <Bot className="size-3" strokeWidth={1.75} />
            </span>
            {parentThreadId && onThread ? (
              <button
                type="button"
                onClick={() => onThread(parentThreadId)}
                title={
                  parentThread
                    ? threadTitle(parentThread)
                    : "Open parent thread"
                }
                className="group/parent flex items-center gap-1 rounded text-xs font-medium text-foreground hover:underline hover:underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring"
              >
                Parent agent
                <ArrowUpLeft
                  aria-hidden
                  className="size-3 text-muted-foreground opacity-0 transition-opacity group-hover/parent:opacity-100 group-focus-visible/parent:opacity-100"
                />
              </button>
            ) : (
              <span className="text-xs font-medium text-foreground">
                Parent agent
              </span>
            )}
          </>
        ) : (
          <>
            <span className="sr-only">{group.role}</span>
            {group.author || group.origin ? (
              <span>
                {group.author}
                {group.author && group.origin ? " · " : ""}
                {group.origin
                  ? `via ${{ slack: "Slack", github: "GitHub", ui: "UI", cron: "Schedule" }[group.origin]}`
                  : ""}
              </span>
            ) : null}
          </>
        )}
        {group.pending ? (
          <span
            role="status"
            aria-label="Message queued"
            className="text-[11px] text-muted-foreground"
          >
            Queued
          </span>
        ) : showTimestamp ? (
          <time
            dateTime={validDate.toISOString()}
            title={validDate.toLocaleString()}
          >
            {showDate ? `${dateFormat.format(validDate)} · ` : ""}
            {timeFormat.format(validDate)}
          </time>
        ) : null}
        {thinking ? (
          <span
            role="status"
            aria-label="Assistant is thinking"
            className={activeStatusClassName}
          >
            {thinkingLabel}
          </span>
        ) : null}
      </div>
      {group.role === "user" || group.role === "parent" ? (
        <div
          className={
            group.role === "parent"
              ? "w-fit max-w-full space-y-3 rounded-lg border border-border bg-muted/30 px-3.5 py-2.5"
              : "w-fit max-w-full space-y-3 rounded-2xl bg-muted/70 px-4 py-3"
          }
        >
          {items}
        </div>
      ) : (
        <>
          {handoffs}
          {activity}
          {items}
        </>
      )}
    </article>
  );
});

export const Transcript = memo(function Transcript({
  messages,
  toolProgress,
  stopped = false,
  live = "",
  liveTurn,
  owner,
  pendingTurn,
  responding = false,
  respondingLabel,
  threads,
  parentThread,
  parentThreadId,
  onThread,
}: {
  messages: Message[];
  toolProgress?: ToolProgress;
  stopped?: boolean;
  live?: string;
  liveTurn?: number;
  owner?: string;
  pendingTurn?: number;
  responding?: boolean;
  respondingLabel?: string;
} & ThreadContext) {
  const journalGroups = useMemo(
    () => groupMessages(messages, toolProgress, stopped, parentThreadId),
    [messages, toolProgress, stopped, parentThreadId],
  );
  const groups = useMemo(
    () => collectTaskConversations(journalGroups, threads),
    [journalGroups, threads],
  );
  const last = journalGroups.at(-1);
  // New prompt text can still be uncommitted when its response starts streaming.
  // Continue a tool exchange only if its group remains in the main transcript.
  // Directed task activity may have moved entirely into an earlier board.
  const continuing =
    last?.role === "assistant" &&
    last.activity.at(-1)?.kind === "tool" &&
    groups.some((group) => group.key === last.key);
  const activeTurn = liveTurn ?? pendingTurn;
  const liveGroup =
    activeTurn === undefined
      ? emptyReply
      : {
          ...emptyReply,
          key: `turn:${activeTurn}`,
          ...((last?.role === "user" || last?.role === "parent") &&
          last.timestamp !== undefined
            ? { startedAt: last.timestamp }
            : {}),
        };
  const liveKey =
    liveTurn === undefined ? "live:text" : `turn:${liveTurn}:text`;
  const renderedGroups = groups.map((group, index) => (
    <MessageGroup
      key={group.key}
      group={group}
      active={
        responding &&
        group.key === last?.key &&
        group.role === "assistant" &&
        group.activity.length > 0
      }
      showDate={
        displayTimestamp(group) !== undefined &&
        (index === 0 ||
          displayTimestamp(groups[index - 1]) === undefined ||
          new Date(displayTimestamp(group)! * 1000).toDateString() !==
            new Date(
              displayTimestamp(groups[index - 1])! * 1000,
            ).toDateString())
      }
      live={group.key === last?.key && continuing ? live : ""}
      liveKey={liveKey}
      owner={owner}
      threads={threads}
      parentThread={parentThread}
      parentThreadId={parentThreadId}
      onThread={onThread}
    />
  ));
  if ((live || responding) && !continuing) {
    renderedGroups.push(
      <MessageGroup
        key={liveGroup.key}
        group={liveGroup}
        live={live}
        liveKey={liveKey}
        owner={owner}
        active={responding}
        thinkingLabel={respondingLabel}
      />,
    );
  }
  return <div className="flex flex-col gap-6">{renderedGroups}</div>;
});
