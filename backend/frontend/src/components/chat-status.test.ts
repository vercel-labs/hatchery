import assert from "node:assert/strict";
import test from "node:test";

import { submissionLabel } from "./chat-status.ts";

test("shows agent assignment while the persisted chat is unassigned", () => {
  assert.equal(submissionLabel(null), "Assigning an agent…");
});

test("shows thinking once the persisted chat has an agent", () => {
  assert.equal(submissionLabel("agent-1"), "Thinking…");
});
