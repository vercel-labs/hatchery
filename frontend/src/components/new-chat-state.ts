import type { Chat } from "@/lib/api";

export const AUTO_SPACE_VALUE = "__auto__";

export type NewChatRequest = {
  id: string;
  space_id?: string;
};

export type NewChatHandoff = {
  message: {
    id: string;
    role: "user";
    parts: [{ type: "text"; text: string }];
  };
  loadMessages: false;
  resumeOnMount: false;
  suppressNextStream: true;
};

export function newChatId(): string {
  return `chat_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
}

export function newChatHandoff(
  text: string,
  messageId = `msg_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`,
): NewChatHandoff {
  return {
    message: {
      id: messageId,
      role: "user",
      parts: [{ type: "text", text }],
    },
    loadMessages: false,
    resumeOnMount: false,
    suppressNextStream: true,
  };
}

export function startsFreshDraft(previousPath: string | null, path: string): boolean {
  return path === "/" && previousPath !== "/";
}

export function streamAttachmentAction(
  attachedGeneration: number,
  announcedGeneration: number,
  status: "submitted" | "streaming" | "ready" | "error",
  suppressNextStream: boolean,
): "ignore" | "wait" | "suppress" | "resume" {
  if (announcedGeneration <= attachedGeneration) return "ignore";
  if (status === "submitted" || status === "streaming") return "wait";
  if (status === "error") return "resume";
  return suppressNextStream ? "suppress" : "resume";
}

export function newChatRequest(
  id: string,
  spaceId: string | null,
): NewChatRequest {
  return { id, ...(spaceId ? { space_id: spaceId } : {}) };
}

export function createChatPersister(
  create: (request: NewChatRequest) => Promise<Chat>,
  id = newChatId(),
): (spaceId: string | null) => Promise<Chat> {
  let pending: Promise<Chat> | null = null;
  let request: NewChatRequest | null = null;

  return (spaceId) => {
    if (pending) return pending;
    request ??= newChatRequest(id, spaceId);
    const attempt = create(request);
    pending = attempt;
    attempt.catch(() => {
      if (pending === attempt) pending = null;
    });
    return pending;
  };
}

export function isBrandNewChat(messageCount: number): boolean {
  return messageCount === 0;
}
