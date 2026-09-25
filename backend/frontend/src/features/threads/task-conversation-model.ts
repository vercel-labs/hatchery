import type { Thread } from "@/lib/api-types";
import type {
  Group,
  Item,
  TaskConversationItem,
  TaskItem,
} from "./transcript-model";

export function taskArguments(value: unknown): Record<string, unknown> {
  try {
    const parsed: unknown =
      typeof value === "string" ? JSON.parse(value) : value;
    return parsed && typeof parsed === "object"
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

export function taskField(text: string, field: string): string {
  return (
    text.match(new RegExp(`^${field}:[ \\t]*([^\\n]*)`, "m"))?.[1]?.trim() ?? ""
  );
}

function taskHandle(item: TaskItem): string {
  return (
    item.handle ||
    taskField(item.text, "Task") ||
    taskField(item.text, "Task ID") ||
    (/^task-\d+$/.test(item.taskId ?? "") ? item.taskId! : "")
  );
}

const directedTools = new Set([
  "delegate",
  "message_task",
  "cancel_task",
  "archive_task",
  "review_proposal",
]);

// Only explicit task inputs and directed tool calls belong in a task conversation.
// Ordinary replies stay operator-facing, regardless of what triggered the turn.
// Journal order, rather than timestamps or mutable roster summaries, defines history.
export function collectTaskConversations(
  groups: Group[],
  threads: Thread[] = [],
): Group[] {
  const ordered = groups
    .flatMap((group) => [...group.activity, ...group.items])
    .sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
  const aliases = new Map<string, Set<string>>();
  function register(keys: string[]) {
    const merged = new Set(keys);
    for (const key of keys)
      for (const alias of aliases.get(key) ?? []) merged.add(alias);
    for (const key of merged) aliases.set(key, merged);
  }
  function taskKeys(handle?: string, taskId?: string, threadId?: string) {
    return [
      handle && `task:${handle}`,
      taskId && `task:${taskId}`,
      threadId && `thread:${threadId}`,
    ].filter((key): key is string => Boolean(key));
  }
  for (const thread of threads)
    register(taskKeys(thread.task_handle, thread.task_id, thread.thread_id));
  for (const item of ordered) {
    if (item.kind === "task")
      register(taskKeys(taskHandle(item), item.taskId, item.threadId));
  }

  const conversations = new Map<Set<string> | string, TaskConversationItem>();
  const byItem = new Map<string, TaskConversationItem>();
  for (const item of ordered) {
    let keys: string[];
    let handle = "";
    let objective = "";
    if (item.kind === "task") {
      handle = taskHandle(item);
      keys = taskKeys(handle, item.taskId, item.threadId);
      objective = taskField(item.text, "Objective");
    } else if (item.kind === "tool" && directedTools.has(item.name)) {
      const args = taskArguments(item.call?.tool_args);
      const result = taskArguments(item.result?.result);
      handle = String(result.task_id ?? args.task_id ?? "");
      keys = taskKeys(handle);
      objective = item.name === "delegate" ? String(args.objective ?? "") : "";
      if (!keys.length && item.name !== "delegate") continue;
    } else continue;

    const identity = aliases.get(keys[0]) ?? keys[0] ?? `item:${item.key}`;
    let conversation = conversations.get(identity);
    if (!conversation) {
      const child = threads.find((thread) =>
        taskKeys(thread.task_handle, thread.task_id, thread.thread_id).some(
          (key) =>
            identity instanceof Set ? identity.has(key) : identity === key,
        ),
      );
      conversation = {
        kind: "task-conversation",
        key: item.key,
        handle: child?.task_handle || handle,
        threadId: child?.thread_id,
        objective: objective || child?.task_objective || "",
        timestamp: item.timestamp,
        sequence: item.sequence,
        entries: [],
      };
      conversations.set(identity, conversation);
    }
    if (item.kind === "task") {
      conversation.threadId ||= item.threadId;
      conversation.handle = taskHandle(item) || conversation.handle;
    } else if (item.name === "delegate") conversation.delegation = item;
    conversation.objective ||= objective;
    conversation.entries.push(item);
    byItem.set(item.key, conversation);
  }

  function project(items: Item[]): Item[] {
    return items.flatMap((item): Item[] => {
      const conversation = byItem.get(item.key);
      if (!conversation) return [item];
      return conversation.key === item.key ? [conversation] : [];
    });
  }

  return groups
    .map((group) => ({
      ...group,
      activity: project(group.activity),
      items: project(group.items),
    }))
    .filter((group) => group.activity.length || group.items.length);
}
