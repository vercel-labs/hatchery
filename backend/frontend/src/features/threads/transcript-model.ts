import type { Message, TaskStatus, ToolProgress } from "@/lib/api-types";

export type Part = Record<string, unknown>;
type ItemPosition = { timestamp?: number; sequence?: number };
export type ToolItem = ItemPosition & {
  kind: "tool";
  key: string;
  name: string;
  call?: Part;
  result?: Part;
  execution?: "running" | "queued" | "stopped";
};
export type ApiItem = ItemPosition & {
  kind: "api";
  key: string;
  text: string;
  source: "api" | "schedule";
  requestId?: string;
};
export type TaskItem = ItemPosition & {
  kind: "task";
  key: string;
  text: string;
  taskId?: string;
  threadId?: string;
  handle?: string;
  status?: TaskStatus;
  event?: "message" | "completion";
};
export type TextItem = ItemPosition & {
  kind: "text" | "thought" | "control";
  key: string;
  text: string;
  // Hatchery: text still arriving on the live UI message stream.
  streaming?: boolean;
};
export type TaskConversationItem = ItemPosition & {
  kind: "task-conversation";
  key: string;
  handle: string;
  threadId?: string;
  objective: string;
  delegation?: ToolItem;
  entries: Array<ToolItem | TaskItem>;
};
export type Item =
  ToolItem | ApiItem | TaskItem | TextItem | TaskConversationItem;
export type Group = {
  key: string;
  role: "user" | "assistant" | "parent";
  activity: Item[];
  items: Item[];
  timestamp?: number;
  startedAt?: number;
  completedAt?: number;
  pending?: "sending" | "queued";
  // Hatchery: the human author and channel of a user message.
  author?: string;
  origin?: Message["origin"];
};

const inlineTools = new Set([
  "signal",
  "cancel",
  "message_parent",
  "secret_request",
]);
const autonomousSources = new Set(["api", "schedule", "signal", "task"]);

export function groupMessages(
  messages: Message[],
  progress?: ToolProgress,
  stopped = false,
  parentThreadId?: string,
): Group[] {
  const groups: Group[] = [];
  const calls = new Map<string, ToolItem>();
  let pendingStartedAt: number | undefined;
  let sequence = 0;
  const firstPrompt = messages.findIndex(
    (message) =>
      message.role === "user" &&
      message.source !== "maintenance" &&
      !autonomousSources.has(message.source ?? ""),
  );
  for (const [messageIndex, message] of messages.entries()) {
    if (message.role === "system" || message.source === "maintenance") continue;
    if (message.role === "user" && !autonomousSources.has(message.source ?? ""))
      pendingStartedAt = message.timestamp;
    // Older delegated starts were tagged operator. Only the initial, request-less
    // assignment can be recovered from the thread's durable parent relationship.
    const fromParent =
      message.source === "parent" ||
      Boolean(
        parentThreadId &&
        messageIndex === firstPrompt &&
        !message.request_id &&
        (!message.source || message.source === "operator"),
      );
    const role = fromParent
      ? "parent"
      : message.role === "user" && !autonomousSources.has(message.source ?? "")
        ? "user"
        : "assistant";
    let group = role === "assistant" ? groups.at(-1) : undefined;
    if (group?.role !== role) group = undefined;
    if (autonomousSources.has(message.source ?? "")) group = undefined;
    // Keep tool activity together, but give a delayed autonomous reply its own time.
    if (
      group &&
      role === "assistant" &&
      message.timestamp !== undefined &&
      group.timestamp !== undefined &&
      message.timestamp - group.timestamp >= 300
    )
      group = undefined;

    const requestKey =
      message.role === "user" && message.request_id
        ? `request:${message.request_id}`
        : undefined;
    const turnKey =
      role === "assistant" && message.turn !== undefined
        ? `turn:${message.turn}`
        : undefined;
    const hasWorkTool = (message.parts ?? []).some(
      (part) =>
        part.kind === "tool_call" &&
        typeof part.tool_name === "string" &&
        part.tool_name !== "idle" &&
        !inlineTools.has(part.tool_name),
    );
    let textIndex = 0;

    for (const [partIndex, part] of (message.parts ?? []).entries()) {
      const fallbackKey = String(part.id ?? `${messageIndex}:${partIndex}`);
      const stableTextKey = requestKey ?? turnKey;
      const currentTextIndex = part.kind === "text" ? textIndex++ : -1;
      const key =
        stableTextKey && currentTextIndex >= 0
          ? `${stableTextKey}:text${currentTextIndex ? `:${currentTextIndex}` : ""}`
          : fallbackKey;
      let item: Item;
      if (part.kind === "text" && typeof part.text === "string" && part.text) {
        if (message.source === "api" || message.source === "schedule") {
          item = {
            kind: "api",
            key,
            text: part.text,
            source: message.source,
            ...(message.request_id ? { requestId: message.request_id } : {}),
          };
        } else if (message.source === "task") {
          item = {
            kind: "task",
            key,
            text: part.text,
            taskId: message.task_id,
            threadId: message.reporting_thread_id,
            handle: message.task_handle,
            status: message.task_status,
            event: message.task_event,
          };
        } else {
          item = {
            kind:
              message.source === "signal"
                ? "control"
                : role === "assistant" && hasWorkTool
                  ? "thought"
                  : "text",
            key,
            text: part.text,
            ...(part.streaming === true ? { streaming: true } : {}),
          };
        }
      } else if (
        (part.kind === "tool_call" || part.kind === "tool_result") &&
        typeof part.tool_name === "string"
      ) {
        if (part.tool_name === "idle") continue;
        const id =
          typeof part.tool_call_id === "string" ? part.tool_call_id : undefined;
        const call = id ? calls.get(id) : undefined;
        if (
          part.kind === "tool_result" &&
          id &&
          call &&
          call.name === part.tool_name
        ) {
          call.result = part;
          calls.delete(id);
          continue;
        }
        item = {
          kind: "tool",
          key,
          name: part.tool_name,
          ...(part.kind === "tool_call" ? { call: part } : { result: part }),
        };
        if (part.kind === "tool_call" && id) calls.set(id, item);
      } else continue;

      item.timestamp = message.timestamp;
      item.sequence = sequence++;

      if (!group) {
        group = {
          key: requestKey ?? turnKey ?? key,
          role,
          activity: [],
          items: [],
          timestamp: message.timestamp,
          ...(role === "assistant" && pendingStartedAt !== undefined
            ? { startedAt: pendingStartedAt }
            : {}),
          pending: message.pending,
          ...(role === "user" && message.author ? { author: message.author } : {}),
          ...(role === "user" && message.origin ? { origin: message.origin } : {}),
        };
        groups.push(group);
        if (role === "assistant") pendingStartedAt = undefined;
      }
      // Messages to the parent are part of the conversation, not hidden work.
      if (
        item.kind === "text" ||
        item.kind === "task" ||
        (item.kind === "tool" &&
          (item.name === "secret_request" || item.name === "message_parent"))
      )
        group.items.push(item);
      else group.activity.push(item);
    }
    if (group && role === "assistant" && message.timestamp !== undefined)
      group.completedAt = message.timestamp;
  }
  // Child results are durable before the batch is appended to the Journal.
  // Match them to existing cards; the later Journal result takes precedence.
  for (const result of progress?.results ?? []) {
    const id = result.tool_call_id;
    const call = typeof id === "string" ? calls.get(id) : undefined;
    if (call && call.name === result.tool_name) call.result = result;
  }
  const queued = new Set(progress?.queued);
  for (const [id, item] of calls) {
    if (item.result) continue;
    if (stopped) item.execution = "stopped";
    else if (id === progress?.running) item.execution = "running";
    else if (queued.has(id)) item.execution = "queued";
  }
  return groups;
}
