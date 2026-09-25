import assert from "node:assert/strict";
import test from "node:test";

import type { Chat } from "./api.ts";
import type { Thread } from "./api-types.ts";
import { chatAttentionLabel, chatSidebarText, sidebarThreads } from "./chat-sidebar.ts";

function chat(overrides: Partial<Chat> = {}): Chat {
  return {
    id: "chat_1",
    user_id: "user_1",
    author_display_name: "Ada",
    agent_id: null,
    title: "new chat",
    topic: null,
    trigger: "ui",
    status: "queued",
    sandbox_id: null,
    artifact: null,
    attention_reason: null,
    archived_at: null,
    created_at: "2026-09-04T00:00:00Z",
    ...overrides,
  };
}

test("labels persisted attention reasons", () => {
  assert.equal(
    chatAttentionLabel(chat({ attention_reason: "result_available" })),
    "Result available",
  );
  assert.equal(chatAttentionLabel(chat({ attention_reason: "blocked" })), "Blocked");
  assert.equal(chatAttentionLabel(chat()), null);
});

test("combines the persisted author and possessive fragment", () => {
  assert.deepEqual(chatSidebarText(chat({ topic: "'s cron jobs work" })), {
    author: "Ada",
    fragment: "'s cron jobs work",
    label: "Ada's cron jobs work",
  });
});

test("provides the real chat name instead of the placeholder title", () => {
  assert.equal(
    chatSidebarText(
      chat({ title: "new chat", topic: "wants to rewire slack" }),
    ).label,
    "Ada wants to rewire slack",
  );
});

test("uses pending and legacy fallbacks", () => {
  assert.deepEqual(chatSidebarText(chat()), {
    author: "Ada",
    fragment: "…",
    label: "Ada …",
  });
  assert.equal(
    chatSidebarText(chat({ author_display_name: null, topic: "fixes tests" })).label,
    "fixes tests",
  );
  assert.equal(
    chatSidebarText(
      chat({ author_display_name: null, title: "Legacy Slack thread" }),
    ).label,
    "Legacy Slack thread",
  );
  assert.equal(
    chatSidebarText(chat({ author_display_name: null })).label,
    "New chat",
  );
});

function rosterThread(threadId: string, values: Partial<Thread> = {}): Thread {
  return {
    thread_id: threadId,
    parent_thread_id: "",
    depth: 0,
    upstream: "main",
    children: [],
    status: "idle",
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
    ...values,
  };
}

test("builds the agent's thread tree from its root chats and delegated threads", () => {
  const chats = [
    chat({ id: "newer", agent_id: "agent_1", topic: "Newer", created_at: "2026-09-06T00:00:00Z" }),
    chat({ id: "older", agent_id: "agent_1", topic: "Older", trigger: "slack:T1" }),
    chat({ id: "unassigned", agent_id: null, topic: "Classifying" }),
    chat({ id: "other", agent_id: "agent_2" }),
    chat({ id: "shelved", agent_id: "agent_1", archived_at: "2026-09-05T00:00:00Z" }),
  ];
  const threads = [
    rosterThread("root-older", { chat_id: "older", status: "active" }),
    rosterThread("child", { chat_id: "child-chat", parent_thread_id: "root-older", depth: 1 }),
    rosterThread("root-shelved", { chat_id: "shelved" }),
    rosterThread("hidden-child", { chat_id: "hidden", parent_thread_id: "root-shelved" }),
  ];

  const tree = sidebarThreads(chats, threads, "agent_1");

  assert.deepEqual(
    tree.map((thread) => [thread.thread_id, thread.chat_id]),
    [
      ["root-older", "older"],
      ["chat:unassigned", "unassigned"],
      ["chat:newer", "newer"],
      ["child", "child-chat"],
    ],
  );
  assert.equal(tree[0].title, "Ada Older");
  assert.equal(tree[0].trigger, "slack:T1");
  assert.equal(tree[0].status, "active");
  assert.equal(tree[2].status, "idle");
});
