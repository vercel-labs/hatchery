import assert from "node:assert/strict";
import test from "node:test";

import type { Chat } from "../lib/api.ts";
import {
  AUTO_SPACE_VALUE,
  createChatPersister,
  isBrandNewChat,
  newChatRequest,
} from "./new-chat-state.ts";

function chat(id: string): Chat {
  return {
    id,
    user_id: "user_1",
    space_id: null,
    title: "new chat",
    topic: null,
    trigger: "ui",
    status: "queued",
    sandbox_id: null,
    artifact: null,
    attention_reason: null,
    archived_at: null,
    created_at: "2026-09-09T00:00:00Z",
  };
}

test("builds a retry-safe new chat request with an optional draft space", () => {
  assert.deepEqual(newChatRequest("chat_123456789abc", null), {
    id: "chat_123456789abc",
  });
  assert.deepEqual(newChatRequest("chat_123456789abc", "space_1"), {
    id: "chat_123456789abc",
    space_id: "space_1",
  });
  assert.equal(AUTO_SPACE_VALUE, "__auto__");
});

test("coalesces concurrent persistence and keeps the first selected space", async () => {
  const requests: ReturnType<typeof newChatRequest>[] = [];
  let resolve!: (value: Chat) => void;
  const persister = createChatPersister(
    async (request) => {
      requests.push(request);
      return new Promise<Chat>((done) => {
        resolve = done;
      });
    },
    "chat_123456789abc",
  );

  const first = persister("space_1");
  const second = persister("space_2");
  assert.equal(first, second);
  assert.deepEqual(requests, [
    { id: "chat_123456789abc", space_id: "space_1" },
  ]);

  resolve(chat("chat_123456789abc"));
  assert.equal((await first).id, "chat_123456789abc");
});

test("retries failed persistence with the same chat id", async () => {
  const requests: ReturnType<typeof newChatRequest>[] = [];
  const persister = createChatPersister(
    async (request) => {
      requests.push(request);
      if (requests.length === 1) throw new Error("offline");
      return chat(request.id);
    },
    "chat_123456789abc",
  );

  await assert.rejects(persister("space_1"), /offline/);
  assert.equal((await persister("space_2")).id, "chat_123456789abc");
  assert.deepEqual(requests, [
    { id: "chat_123456789abc", space_id: "space_1" },
    { id: "chat_123456789abc", space_id: "space_1" },
  ]);
});

test("historical empty chats still use the new-chat layout", () => {
  assert.equal(isBrandNewChat(0), true);
  assert.equal(isBrandNewChat(1), false);
});
