import assert from "node:assert/strict";
import test from "node:test";

import {
  AUTO_SPACE_VALUE,
  isBrandNewChat,
  newChatRequest,
} from "./new-chat-state.ts";

test("a new chat is created without a selected space for automatic routing", () => {
  assert.deepEqual(newChatRequest(), {});
  assert.equal(isBrandNewChat(0), true);
  assert.equal(AUTO_SPACE_VALUE, "__auto__");
});

test("the new-chat layout lasts until the first message", () => {
  assert.equal(isBrandNewChat(0), true);
  assert.equal(isBrandNewChat(1), false);
});
