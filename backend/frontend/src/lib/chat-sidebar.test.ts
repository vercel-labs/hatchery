import assert from "node:assert/strict";
import test from "node:test";

import type { Chat } from "./api.ts";
import {
  chatAttentionFilterLabel,
  chatAttentionLabel,
  chatSidebarText,
  filterSidebarChats,
  selectSidebarSpace,
  type ChatSidebarFilters,
} from "./chat-sidebar.ts";

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

test("uses the Requires attention filter label", () => {
  assert.equal(chatAttentionFilterLabel, "Requires attention");
});

test("filters active chats by attention and one space", () => {
  const chats = [
    chat({ id: "matching", space_id: "space_1", attention_reason: "blocked" }),
    chat({ id: "other-space", space_id: "space_2", attention_reason: "blocked" }),
    chat({ id: "no-attention", space_id: "space_1" }),
    chat({
      id: "archived",
      space_id: "space_1",
      attention_reason: "result_available",
      archived_at: "2026-09-05T00:00:00Z",
    }),
  ];

  assert.deepEqual(
    filterSidebarChats(chats, { requiresAttention: true, spaceId: "space_1" }).map(
      ({ id }) => id,
    ),
    ["matching"],
  );
  assert.deepEqual(
    filterSidebarChats(chats, { requiresAttention: false, spaceId: null }).map(
      ({ id }) => id,
    ),
    ["matching", "other-space", "no-attention"],
  );
});

test("selecting and removing a space filter keeps other filters active", () => {
  const filters: ChatSidebarFilters = {
    requiresAttention: true,
    spaceId: "space_1",
  };

  assert.deepEqual(selectSidebarSpace(filters, "space_2"), {
    requiresAttention: true,
    spaceId: "space_2",
  });
  assert.deepEqual(selectSidebarSpace(filters, null), {
    requiresAttention: true,
    spaceId: null,
  });
});

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
