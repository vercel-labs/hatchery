import type { Chat } from "./api";
import type { Thread } from "./api-types";

export type ChatSidebarText = {
  author: string | null;
  fragment: string;
  label: string;
};

export function chatAttentionLabel(
  chat: Pick<Chat, "attention_reason">,
): string | null {
  if (chat.attention_reason === "result_available") return "Result available";
  if (chat.attention_reason === "blocked") return "Blocked";
  return null;
}

export function chatSidebarText(chat: Chat): ChatSidebarText {
  const author = chat.author_display_name?.trim() || null;
  const topic = chat.topic?.trim();
  const fragment = topic || (chat.title === "new chat" ? "…" : chat.title);

  if (!author) {
    return {
      author: null,
      fragment,
      label: fragment === "…" ? "New chat" : fragment,
    };
  }
  const separator = fragment.startsWith("'") ? "" : " ";
  return { author, fragment, label: `${author}${separator}${fragment}` };
}

// The sidebar thread tree for one agent: its root chats (plus chats still
// waiting for an agent) as root threads, and their delegated threads from the
// agent's roster. A chat whose thread has not started is shown as an idle root.
export function sidebarThreads(
  chats: Chat[],
  threads: Thread[],
  agentId: string | null,
): Thread[] {
  const byChat = new Map(threads.map((thread) => [thread.chat_id, thread]));
  const byId = new Map(threads.map((thread) => [thread.thread_id, thread]));
  const roots = chats
    .filter(
      (chat) =>
        !chat.parent_chat_id &&
        chat.archived_at === null &&
        (chat.agent_id === agentId || chat.agent_id === null),
    )
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .map((chat): Thread => {
      const shown = {
        chat_id: chat.id,
        title: chatSidebarText(chat).label,
        trigger: chat.trigger,
        attention: chat.attention_reason,
      };
      const thread = byChat.get(chat.id);
      if (thread) return { ...thread, ...shown };
      return {
        thread_id: `chat:${chat.id}`,
        parent_thread_id: "",
        depth: 0,
        upstream: "main",
        children: [],
        status: chat.status === "running" ? "active" : "idle",
        live: true,
        archived: false,
        task_id: "",
        task_handle: "",
        task_status: "",
        deliverables: [],
        summary: "",
        result: "",
        error: "",
        input_tokens: 0,
        output_tokens: 0,
        proposals: [],
        ...shown,
      };
    });
  const shownRoots = new Set(roots.map((thread) => thread.thread_id));
  const rootOf = (thread: Thread) => {
    const seen = new Set<string>();
    let current: Thread | undefined = thread;
    while (current?.parent_thread_id && !seen.has(current.thread_id)) {
      seen.add(current.thread_id);
      current = byId.get(current.parent_thread_id);
    }
    return current?.thread_id;
  };
  const children = threads.filter(
    (thread) =>
      thread.parent_thread_id && shownRoots.has(rootOf(thread) ?? ""),
  );
  return [...roots, ...children];
}
