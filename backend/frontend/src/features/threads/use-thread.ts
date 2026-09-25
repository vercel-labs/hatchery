import { useApi } from "@/hooks/use-api";
import type { ThreadDetail } from "@/lib/api-types";

// The chat's thread details. Unlike the agentmesh console there is no polling or
// WebSocket here: the conversation revalidates on Hatchery's chat event stream and
// while its UI message stream delivers the running turn. A chat whose thread has
// not started yet (404) has no details.
export function useThread(chatId: string | null) {
  return useApi<ThreadDetail>(
    chatId ? `/api/chats/${encodeURIComponent(chatId)}/thread` : null,
    0,
    { waitForCreation: true, keepPreviousData: true },
  );
}
