import assert from "node:assert/strict";
import test from "node:test";

import { formatToolPayload } from "./tool-payload.ts";

test("formats nested tool arguments without JSON punctuation", () => {
  assert.equal(
    formatToolPayload({
      title: "Update Hatchery",
      options: { model: "sonnet", retries: 2, enabled: true },
      tags: ["frontend", "tools"],
    }),
    `title: Update Hatchery
options:
  model: sonnet
  retries: 2
  enabled: true
tags:
  - frontend
  - tools`,
  );
});

test("formats arrays of nested objects", () => {
  assert.equal(
    formatToolPayload({
      tasks: [
        { name: "inspect", paths: ["frontend", "backend"] },
        { name: "test", skipped: false },
      ],
    }),
    `tasks:
  - name: inspect
    paths:
      - frontend
      - backend
  - name: test
    skipped: false`,
  );
});

test("omits null fields and empty collections recursively", () => {
  assert.equal(
    formatToolPayload({
      title: "sandbox",
      ports: [],
      branch: null,
      git_sha: null,
      metadata: { labels: [], note: null },
      nested: [{ ignored: null }, { kept: "yes" }],
    }),
    `title: sandbox
nested:
  - kept: yes`,
  );
});

test("indents multiline scripts and removes only surrounding blank lines and common indentation", () => {
  assert.equal(
    formatToolPayload({
      setup_script: "\n    pnpm install\n    pnpm test\n\n      echo done\n",
      task: "Keep this line.\n\nAnd this paragraph.",
    }),
    `setup_script:
  pnpm install
  pnpm test

    echo done
task:
  Keep this line.

  And this paragraph.`,
  );
});

test("places long scalar values beneath their keys", () => {
  assert.equal(
    formatToolPayload({
      repos: ["vercel-labs/hatchery"],
      setup_script:
        "cd /vercel/hatchery && (command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh) && vercel link --yes --project hatchery --scope vercel-internal-playground",
      title: "Render dispatcher tool JSON neatly",
      size: "big",
    }),
    `repos:
  - vercel-labs/hatchery (default branch)
setup_script:
  cd /vercel/hatchery && (command -v uv >/dev/null || curl -LsSf
  https://astral.sh/uv/install.sh | sh) && vercel link --yes --project
  hatchery --scope vercel-internal-playground
title: Render dispatcher tool JSON neatly
size: big`,
  );

  assert.equal(
    formatToolPayload({
      message:
        "This is a deliberately long general value that should be visually separated from its field name.",
    }),
    `message:
  This is a deliberately long general value that should be visually
  separated from its field name.`,
  );
});

test("formats parsed JSON errors", () => {
  assert.equal(
    formatToolPayload(
      '{"message":"failed","details":{"code":500,"causes":["timeout","worker stopped"]}}',
    ),
    `message: failed
details:
  code: 500
  causes:
    - timeout
    - worker stopped`,
  );
});

test("preserves non-JSON error text and meaningful line breaks", () => {
  assert.equal(
    formatToolPayload("Sandbox timed out\nTry again."),
    "Sandbox timed out\nTry again.",
  );
});

test("preserves JSON scalar error text", () => {
  assert.equal(formatToolPayload('"Sandbox timed out"'), '"Sandbox timed out"');
});

test("labels only an unpinned primary repo as using its default branch", () => {
  assert.equal(
    formatToolPayload({
      repos: ["vercel-labs/hatchery", "vercel/ai"],
      branch: null,
      git_sha: null,
    }),
    `repos:
  - vercel-labs/hatchery (default branch)
  - vercel/ai`,
  );

  assert.equal(
    formatToolPayload({
      repos: ["vercel-labs/hatchery"],
      branch: "feature/tools",
      git_sha: null,
    }),
    `repos:
  - vercel-labs/hatchery
branch: feature/tools`,
  );

  assert.equal(
    formatToolPayload({
      repos: ["vercel-labs/hatchery"],
      branch: null,
      git_sha: "abc123",
    }),
    `repos:
  - vercel-labs/hatchery
git_sha: abc123`,
  );
});
