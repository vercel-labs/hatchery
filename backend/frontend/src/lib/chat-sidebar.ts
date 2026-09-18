import type { Thread } from "./api";

export type ChatSidebarText = {
  author: string | null;
  fragment: string;
  label: string;
};

export type ChatSidebarFilters = {
  requiresAttention: boolean;
  agentId: string | null;
};

export const chatAttentionFilterLabel = "Requires attention";

export function filterSidebarChats(
  chats: Thread[],
  filters: ChatSidebarFilters,
): Thread[] {
  return chats.filter(
    (chat) =>
      chat.archived_at === null &&
      (!filters.requiresAttention || chat.attention_reason !== null) &&
      (filters.agentId === null || chat.agent_id === filters.agentId),
  );
}

export function selectSidebarAgent(
  filters: ChatSidebarFilters,
  agentId: string | null,
): ChatSidebarFilters {
  return { ...filters, agentId };
}

export function chatAttentionLabel(chat: Thread): string | null {
  if (chat.attention_reason === "result_available") return "Result available";
  if (chat.attention_reason === "blocked") return "Blocked";
  return null;
}

export function chatSidebarText(chat: Thread): ChatSidebarText {
  const author = chat.author_display_name?.trim() || null;
  const topic = chat.topic?.trim();
  const fragment = topic || (chat.title === "new chat" ? "…" : chat.title);

  if (!author) {
    return {
      author: null,
      fragment,
      label: fragment === "…" ? "New thread" : fragment,
    };
  }
  const separator = fragment.startsWith("'") ? "" : " ";
  return { author, fragment, label: `${author}${separator}${fragment}` };
}
