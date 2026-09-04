import type { Chat } from "./api";

export type ChatSidebarText = {
  author: string | null;
  fragment: string;
  label: string;
};

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
