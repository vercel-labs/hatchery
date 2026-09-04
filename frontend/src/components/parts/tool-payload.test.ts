import assert from "node:assert/strict";
import test from "node:test";

import { formatToolPayload } from "./tool-payload.ts";

test("formats nested tool call arguments with indentation", () => {
  assert.equal(
    formatToolPayload({
      repos: ["vercel/hatchery"],
      options: { model: "sonnet", retries: 2 },
    }),
    `{
  "repos": [
    "vercel/hatchery"
  ],
  "options": {
    "model": "sonnet",
    "retries": 2
  }
}`,
  );
});

test("parses and formats JSON tool error text", () => {
  assert.equal(
    formatToolPayload('{"message":"failed","details":{"code":500}}'),
    `{
  "message": "failed",
  "details": {
    "code": 500
  }
}`,
  );
});

test("preserves non-JSON tool error text", () => {
  assert.equal(formatToolPayload("Sandbox timed out\nTry again."), "Sandbox timed out\nTry again.");
});

test("preserves JSON scalar error text", () => {
  assert.equal(formatToolPayload('"Sandbox timed out"'), '"Sandbox timed out"');
});
