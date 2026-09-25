import { useState } from "react";
import { api, apiFetch } from "@/lib/api";
import { reasonMessage } from "@/lib/format";

// Ported from the agentmesh console. Sending stays with Hatchery's chat stream
// (`useChat`); these are the thread's explicit transitions, addressed by chat.
export function useThreadActions({
  chatId,
  refresh,
  mutate,
}: {
  chatId: string;
  refresh: () => Promise<unknown>;
  mutate: () => Promise<unknown>;
}) {
  const [error, setError] = useState("");
  const [archiving, setArchiving] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [stopping, setStopping] = useState(false);
  const thread = `/api/chats/${encodeURIComponent(chatId)}/thread`;

  // An accepted command stays accepted even if refreshing state fails.
  const settle = () => Promise.allSettled([refresh(), mutate()]);

  async function setArchived(archived: boolean) {
    if (archiving) return;
    setArchiving(true);
    setError("");
    try {
      const response = await apiFetch(
        `/api/chats/${encodeURIComponent(chatId)}/archive`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ archived }),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${response.status}`);
      }
      await settle();
    } catch (reason) {
      setError(reasonMessage(reason));
    } finally {
      setArchiving(false);
    }
  }
  async function resume() {
    if (resuming) return;
    setResuming(true);
    setError("");
    try {
      await api(`${thread}/resume`, { request_id: crypto.randomUUID() });
      await settle();
    } catch (reason) {
      setError(reasonMessage(reason));
    } finally {
      setResuming(false);
    }
  }
  async function stop() {
    if (stopping) return;
    setStopping(true);
    setError("");
    try {
      await api(`${thread}/stop`, { request_id: crypto.randomUUID() });
      await settle();
    } catch (reason) {
      setError(reasonMessage(reason));
    } finally {
      setStopping(false);
    }
  }
  // Pinned to the reviewed commit; a stale SHA needs a new review.
  async function approve(branch: string, sha: string) {
    await api(`${thread}/approve`, { branch, expected_sha: sha });
    await settle();
  }
  return {
    stop,
    setArchived,
    resume,
    approve,
    error,
    archiving,
    resuming,
    stopping,
  };
}
