import assert from "node:assert/strict";
import test from "node:test";

import type { Chat } from "./api.ts";
import { chatAttentionLabel, chatSidebarText } from "./chat-sidebar.ts";

function chat(overrides: Partial<Chat> = {}): Chat {
  return {
    id: "chat_1",
    user_id: "user_1",
    author_display_name: "Ada",
    space_id: null,
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

test("combines the persisted author and verb fragment", () => {
  assert.equal(
    chatSidebarText(chat({ topic: "wants to rewire slack" })).label,
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
