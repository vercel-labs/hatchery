import assert from "node:assert/strict";
import test from "node:test";

import type { ScratchpadView } from "./api.ts";
import {
  draftFromView,
  overwriteExpectedVersion,
  recordScratchpadConflict,
  shouldRenderScratchpadDiff,
} from "./scratchpad-state.ts";

function view(version: number, head = version, unread = false): ScratchpadView {
  return {
    snapshot: {
      version,
      content: `snapshot ${version}`,
      actor: { kind: "user", id: "user_1", name: "Ada" },
      created_at: "2026-09-09T00:00:00Z",
    },
    head_version: head,
    last_read_version: 0,
    unread,
    diff: [],
    diff_truncated: false,
  };
}

test("draft starts from the exact viewed immutable snapshot", () => {
  assert.deepEqual(draftFromView(view(3, 7)), {
    content: "snapshot 3",
    expectedVersion: 3,
    conflictHeadVersion: null,
  });
});

test("conflict preserves the full draft and requires its head for overwrite", () => {
  const draft = { ...draftFromView(view(3)), content: "my complete draft" };
  const conflicted = recordScratchpadConflict(draft, view(8));

  assert.equal(conflicted.content, "my complete draft");
  assert.equal(conflicted.expectedVersion, 3);
  assert.equal(overwriteExpectedVersion(conflicted), 8);
  assert.throws(() => overwriteExpectedVersion(draft), /explicit conflict confirmation/);
});

test("only an unread head renders as a cumulative diff", () => {
  assert.equal(shouldRenderScratchpadDiff(view(4, 4, true)), true);
  assert.equal(shouldRenderScratchpadDiff(view(3, 4, true)), false);
  assert.equal(shouldRenderScratchpadDiff(view(4, 4, false)), false);
});
