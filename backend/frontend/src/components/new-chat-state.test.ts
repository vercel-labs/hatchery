import assert from "node:assert/strict";
import test from "node:test";

import { Chat as SDKChat } from "@ai-sdk/react";
import type { UIMessage } from "ai";

import type { Thread } from "../lib/api.ts";
import {
  AUTO_AGENT_VALUE,
  createChatPersister,
  isBrandNewChat,
  newChatHandoff,
  newChatRequest,
  startsFreshDraft,
  streamAttachmentAction,
} from "./new-chat-state.ts";

function chat(id: string): Thread {
  return {
    id,
    user_id: "user_1",
    agent_id: null,
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

test("builds a retry-safe new chat request with an optional draft agent", () => {
  assert.deepEqual(newChatRequest("chat_123456789abc", null), {
    id: "chat_123456789abc",
  });
  assert.deepEqual(newChatRequest("chat_123456789abc", "agent_1"), {
    id: "chat_123456789abc",
    agent_id: "agent_1",
  });
  assert.equal(AUTO_AGENT_VALUE, "__auto__");
});

test("coalesces concurrent persistence and keeps the first selected agent", async () => {
  const requests: ReturnType<typeof newChatRequest>[] = [];
  let resolve!: (value: Thread) => void;
  const persister = createChatPersister(
    async (request) => {
      requests.push(request);
      return new Promise<Thread>((done) => {
        resolve = done;
      });
    },
    "chat_123456789abc",
  );

  const first = persister("agent_1");
  const second = persister("agent_2");
  assert.equal(first, second);
  assert.deepEqual(requests, [
    { id: "chat_123456789abc", agent_id: "agent_1" },
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

  await assert.rejects(persister("agent_1"), /offline/);
  assert.equal((await persister("agent_2")).id, "chat_123456789abc");
  assert.deepEqual(requests, [
    { id: "chat_123456789abc", agent_id: "agent_1" },
    { id: "chat_123456789abc", agent_id: "agent_1" },
  ]);
});

test("hands the first message to one owned POST without loading or resuming", () => {
  const handoff = newChatHandoff("hello", "msg_123456789abc");

  assert.deepEqual(handoff, {
    message: {
      id: "msg_123456789abc",
      role: "user",
      parts: [{ type: "text", text: "hello" }],
    },
    loadMessages: false,
    resumeOnMount: false,
    suppressNextStream: true,
  });
});

test("the handoff message starts one send and no reconnect", async () => {
  const requests: UIMessage[][] = [];
  let reconnects = 0;
  let closeStream!: () => void;
  const sdkChat = new SDKChat<UIMessage>({
    id: "chat_123456789abc",
    messages: [],
    transport: {
      async sendMessages({ messages }) {
        requests.push(messages);
        return new ReadableStream({
          start(controller) {
            closeStream = () => controller.close();
          },
        });
      },
      async reconnectToStream() {
        reconnects += 1;
        return null;
      },
    },
  });
  const handoff = newChatHandoff("hello", "msg_123456789abc");

  const sending = sdkChat.sendMessage(handoff.message);
  await new Promise((resolve) => setTimeout(resolve));

  assert.equal(requests.length, 1);
  assert.equal(reconnects, 0);
  assert.equal(sdkChat.status, "submitted");
  assert.equal(requests[0][0].id, handoff.message.id);
  assert.equal(requests[0][0].role, "user");
  assert.deepEqual(requests[0][0].parts, handoff.message.parts);
  assert.equal(sdkChat.messages[0].id, handoff.message.id);

  closeStream();
  await sending;
  assert.equal(sdkChat.status, "ready");
});

test("recognizes every navigation into the root as a fresh draft", () => {
  assert.equal(startsFreshDraft(null, "/"), true);
  assert.equal(startsFreshDraft("/chats/chat_1", "/"), true);
  assert.equal(startsFreshDraft("/agents/agent_1", "/"), true);
  assert.equal(startsFreshDraft("/", "/"), false);
  assert.equal(startsFreshDraft("/", "/chats/chat_1"), false);
});

test("settles a locally owned stream without replay and resumes on error", () => {
  assert.equal(streamAttachmentAction(0, 1, "submitted", true), "wait");
  assert.equal(streamAttachmentAction(0, 1, "streaming", true), "wait");
  assert.equal(streamAttachmentAction(0, 1, "ready", true), "suppress");
  assert.equal(streamAttachmentAction(0, 1, "error", true), "resume");
  assert.equal(streamAttachmentAction(1, 1, "ready", false), "ignore");
  assert.equal(streamAttachmentAction(1, 2, "ready", false), "resume");
});

test("empty threads still use the new-chat layout", () => {
  assert.equal(isBrandNewChat(0), true);
  assert.equal(isBrandNewChat(1), false);
});
