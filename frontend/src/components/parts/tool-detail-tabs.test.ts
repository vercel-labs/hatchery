import assert from "node:assert/strict";
import test from "node:test";

import { toolDetailTabs } from "./tool-detail-tabs.ts";

test("offers input and output details for a completed tool", () => {
  assert.deepEqual(toolDetailTabs({ hasInput: true, result: "output" }), [
    "input",
    "output",
  ]);
});

test("replaces output with error details for a failed tool", () => {
  assert.deepEqual(toolDetailTabs({ hasInput: true, result: "error" }), [
    "input",
    "error",
  ]);
});

test("omits unavailable detail tabs", () => {
  assert.deepEqual(toolDetailTabs({ hasInput: true, result: null }), ["input"]);
  assert.deepEqual(toolDetailTabs({ hasInput: false, result: "output" }), [
    "output",
  ]);
});
