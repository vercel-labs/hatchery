import { act, renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useThreadActions } from "@/features/threads/use-thread-actions";

// Ported from agentmesh console tests/thread-actions.test.tsx. Sending moved to
// Hatchery's chat stream, so the send-guard test has no Hatchery counterpart:
// a chat without an agent is valid and the classifier assigns one. The
// "accepted command survives a failed refresh" property is kept for stop.

it("keeps an accepted stop even when refreshing the roster fails", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({ request_id: "stop-request", outcome: "delivered" }),
    ),
  );
  const { result } = renderHook(() =>
    useThreadActions({
      chatId: "chat_launch",
      refresh: async () => {
        throw new Error("Roster temporarily unavailable");
      },
      mutate: async () => {},
    }),
  );
  await act(async () => {
    await result.current.stop();
  });
  expect(result.current.error).toBe("");
  expect(result.current.stopping).toBe(false);
});

it("stops a working thread and refreshes its state", async () => {
  const fetch = vi.fn(async () =>
    Response.json({ request_id: "stop-request", outcome: "delivered" }),
  );
  vi.stubGlobal("fetch", fetch);
  const refresh = vi.fn(async () => {});
  const mutate = vi.fn(async () => {});
  const { result } = renderHook(() =>
    useThreadActions({ chatId: "chat_one", refresh, mutate }),
  );

  await act(async () => {
    await result.current.stop();
  });

  expect(fetch).toHaveBeenCalledWith(
    "/api/chats/chat_one/thread/stop",
    expect.objectContaining({ method: "POST", credentials: "include" }),
  );
  expect(refresh).toHaveBeenCalledOnce();
  expect(mutate).toHaveBeenCalledOnce();
  expect(result.current.error).toBe("");
});

it("pins approval to the reviewed commit and shows a rejected resume", async () => {
  const fetch = vi.fn(async (path: string) =>
    path.endsWith("/resume")
      ? Response.json({ detail: "the chat has no thread yet" }, { status: 404 })
      : Response.json({ branch: "b", commit: "c".repeat(40) }),
  );
  vi.stubGlobal("fetch", fetch);
  const { result } = renderHook(() =>
    useThreadActions({
      chatId: "chat_one",
      refresh: async () => {},
      mutate: async () => {},
    }),
  );
  await act(async () => {
    await result.current.approve("consolidations/mira/workspace/x", "a".repeat(40));
    await result.current.resume();
  });
  const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
  expect(JSON.parse(String(init.body))).toEqual({
    branch: "consolidations/mira/workspace/x",
    expected_sha: "a".repeat(40),
  });
  expect(result.current.error).toBe("the chat has no thread yet");
});
