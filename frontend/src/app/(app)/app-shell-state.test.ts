import assert from "node:assert/strict";
import test from "node:test";

import { terminalVisibilityAfterSandboxLoad } from "./app-shell-state.ts";

test("opens the sandbox pane when a chat first loads with sandboxes", () => {
  assert.deepEqual(
    terminalVisibilityAfterSandboxLoad({}, "chat-1", true),
    { "chat-1": true },
  );
});

test("keeps a manually closed sandbox pane closed on refresh", () => {
  const visibility = { "chat-1": false };

  assert.equal(
    terminalVisibilityAfterSandboxLoad(visibility, "chat-1", true),
    visibility,
  );
});

test("keeps visibility separate for each chat", () => {
  assert.deepEqual(
    terminalVisibilityAfterSandboxLoad({ "chat-1": false }, "chat-2", true),
    { "chat-1": false, "chat-2": true },
  );
});
