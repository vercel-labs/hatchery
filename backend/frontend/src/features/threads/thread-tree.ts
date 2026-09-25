import type { AgentThreads, Thread } from "@/lib/api-types";

export type TreeThread = {
  thread: Thread;
  depth: number;
  hasChildren: boolean;
  forceExpanded: boolean;
};

export function logicalThreads(
  threads: Thread[],
  selected: string | null,
  search: string,
): TreeThread[] {
  const roster: Thread[] = [];
  const byId = new Map<string, Thread>();
  for (const thread of threads) {
    if (byId.has(thread.thread_id)) continue;
    byId.set(thread.thread_id, thread);
    roster.push(thread);
  }

  const included = new Set<string>();
  const forced = new Set<string>();
  for (const thread of roster) {
    if (
      (!search && thread.thread_id === selected) ||
      (!thread.archived &&
        (!search ||
          `${threadTitle(thread)} ${thread.summary}`
            .toLowerCase()
            .includes(search)))
    ) {
      included.add(thread.thread_id);
    }
  }

  for (const thread of roster) {
    if (!included.has(thread.thread_id)) continue;
    const revealPath = Boolean(search) || thread.thread_id === selected;
    const path = new Set<string>();
    let current: Thread | undefined = thread;
    while (current && !path.has(current.thread_id)) {
      included.add(current.thread_id);
      path.add(current.thread_id);
      const parent: Thread | undefined = current.parent_thread_id
        ? byId.get(current.parent_thread_id)
        : undefined;
      if (parent && (revealPath || !included.has(parent.thread_id))) {
        forced.add(parent.thread_id);
      }
      current = parent;
    }
  }

  const children = new Map<string, Thread[]>();
  const parentOf = (thread: Thread) => {
    if (
      !thread.parent_thread_id ||
      thread.parent_thread_id === thread.thread_id
    )
      return undefined;
    const parent = byId.get(thread.parent_thread_id);
    return parent && included.has(parent.thread_id) ? parent : undefined;
  };
  for (const thread of roster) {
    if (!included.has(thread.thread_id)) continue;
    const parent = parentOf(thread);
    if (!parent) continue;
    const siblings = children.get(parent.thread_id) ?? [];
    siblings.push(thread);
    children.set(parent.thread_id, siblings);
  }

  const result: TreeThread[] = [];
  const visited = new Set<string>();
  const append = (root: Thread) => {
    const pending: Array<{ thread: Thread; depth: number }> = [
      { thread: root, depth: 0 },
    ];
    while (pending.length) {
      const item = pending.pop()!;
      if (visited.has(item.thread.thread_id)) continue;
      visited.add(item.thread.thread_id);
      const nested = children.get(item.thread.thread_id) ?? [];
      result.push({
        ...item,
        hasChildren: nested.length > 0,
        forceExpanded: forced.has(item.thread.thread_id),
      });
      for (let index = nested.length - 1; index >= 0; index -= 1) {
        pending.push({ thread: nested[index], depth: item.depth + 1 });
      }
    }
  };

  for (const thread of [...roster].reverse()) {
    if (included.has(thread.thread_id) && !parentOf(thread)) append(thread);
  }

  // A component made entirely of parent cycles has no natural root. Promote one
  // cycle member, then traverse the component with the same visited guard.
  for (const thread of [...roster].reverse()) {
    if (!included.has(thread.thread_id) || visited.has(thread.thread_id))
      continue;
    const path = new Set<string>();
    let root = thread;
    while (!visited.has(root.thread_id) && !path.has(root.thread_id)) {
      path.add(root.thread_id);
      const parent = parentOf(root);
      if (!parent) break;
      root = parent;
    }
    append(root);
    if (!visited.has(thread.thread_id)) append(thread);
  }
  return result;
}

export function threadTitle(thread: Thread): string {
  if (thread.title) return thread.title;
  const objective = thread.task_objective?.split("\n")[0]?.trim();
  return (
    objective?.match(/^(.+?[.!?])(?:\s|$)/)?.[1] ||
    objective ||
    thread.summary ||
    "New conversation"
  );
}

export function threadsWithObjectives(agent?: AgentThreads): Thread[] {
  if (!agent) return [];
  const objectives = new Map(
    Object.values(agent.tasks ?? {}).flatMap((task) =>
      typeof task.thread_id === "string" && typeof task.objective === "string"
        ? [[task.thread_id, task.objective] as const]
        : [],
    ),
  );
  return agent.threads.map((thread) => ({
    ...thread,
    task_objective: thread.parent_thread_id
      ? objectives.get(thread.thread_id)
      : undefined,
  }));
}

export function subtreeThreads(threads: Thread[], rootId: string): Thread[] {
  const children = new Map<string, Thread[]>();
  const byId = new Map(threads.map((thread) => [thread.thread_id, thread]));
  for (const thread of byId.values()) {
    const siblings = children.get(thread.parent_thread_id) ?? [];
    siblings.push(thread);
    children.set(thread.parent_thread_id, siblings);
  }
  const result: Thread[] = [];
  const visited = new Set<string>();
  const pending = rootId ? [rootId] : [];
  while (pending.length) {
    const id = pending.pop()!;
    if (visited.has(id)) continue;
    visited.add(id);
    const thread = byId.get(id);
    if (!thread) continue;
    result.push(thread);
    for (const child of [...(children.get(id) ?? [])].reverse())
      pending.push(child.thread_id);
  }
  return result;
}

// Bound indentation while retaining aria-level for deeper delegated trees.
export const treeIndent = [
  "ps-0",
  "ps-3",
  "ps-6",
  "ps-9",
  "ps-12",
  "ps-15",
  "ps-18",
];
