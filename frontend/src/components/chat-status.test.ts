import assert from "node:assert/strict";
import test from "node:test";

import { submissionLabel } from "./chat-status.ts";

test("shows space assignment while the persisted chat is spaceless", () => {
  assert.equal(submissionLabel(null), "Assigning a space…");
});

test("shows thinking once the persisted chat has a space", () => {
  assert.equal(submissionLabel("space-1"), "Thinking…");
});
