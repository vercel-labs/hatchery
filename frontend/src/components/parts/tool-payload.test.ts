import assert from "node:assert/strict";
import test from "node:test";

import { toolPayload } from "./tool-payload.ts";

test("builds nested objects and arrays", () => {
  assert.deepEqual(
    toolPayload({
      title: "Update Hatchery",
      options: { model: "sonnet", retries: 2, enabled: true },
      tags: ["frontend", "tools"],
    }),
    {
      type: "object",
      entries: [
        ["title", { type: "scalar", value: "Update Hatchery" }],
        [
          "options",
          {
            type: "object",
            entries: [
              ["model", { type: "scalar", value: "sonnet" }],
              ["retries", { type: "scalar", value: "2" }],
              ["enabled", { type: "scalar", value: "true" }],
            ],
          },
        ],
        [
          "tags",
          {
            type: "array",
            values: [
              { type: "scalar", value: "frontend" },
              { type: "scalar", value: "tools" },
            ],
          },
        ],
      ],
    },
  );
});

test("keeps long and multiline strings unchanged for CSS wrapping", () => {
  const value = "foo: baralskjflajsf;ajfalskdjlajsdfaljdflasjflajdflajsdf";
  const script = "\n    pnpm install\n      pnpm test\n";

  assert.deepEqual(toolPayload({ value, script }), {
    type: "object",
    entries: [
      ["value", { type: "scalar", value }],
      ["script", { type: "scalar", value: script }],
    ],
  });
});

test("omits null fields and empty collections recursively", () => {
  assert.deepEqual(
    toolPayload({
      title: "sandbox",
      ports: [],
      branch: null,
      metadata: { labels: [], note: null },
      nested: [{ ignored: null }, { kept: "yes" }],
    }),
    {
      type: "object",
      entries: [
        ["title", { type: "scalar", value: "sandbox" }],
        [
          "nested",
          {
            type: "array",
            values: [
              {
                type: "object",
                entries: [["kept", { type: "scalar", value: "yes" }]],
              },
            ],
          },
        ],
      ],
    },
  );
});

test("parses JSON objects but preserves plain text and JSON scalars", () => {
  assert.deepEqual(toolPayload('{"message":"failed","code":500}'), {
    type: "object",
    entries: [
      ["message", { type: "scalar", value: "failed" }],
      ["code", { type: "scalar", value: "500" }],
    ],
  });
  assert.deepEqual(toolPayload("Sandbox timed out\nTry again."), {
    type: "scalar",
    value: "Sandbox timed out\nTry again.",
  });
  assert.deepEqual(toolPayload('"Sandbox timed out"'), {
    type: "scalar",
    value: '"Sandbox timed out"',
  });
});

test("labels only an unpinned primary repo as using its default branch", () => {
  assert.deepEqual(
    toolPayload({
      repos: ["vercel-labs/hatchery", "vercel/ai"],
      branch: null,
      git_sha: null,
    }),
    {
      type: "object",
      entries: [
        [
          "repos",
          {
            type: "array",
            values: [
              {
                type: "scalar",
                value: "vercel-labs/hatchery (default branch)",
              },
              { type: "scalar", value: "vercel/ai" },
            ],
          },
        ],
      ],
    },
  );

  assert.deepEqual(toolPayload({ repos: ["vercel-labs/hatchery"], branch: "dev" }), {
    type: "object",
    entries: [
      [
        "repos",
        {
          type: "array",
          values: [{ type: "scalar", value: "vercel-labs/hatchery" }],
        },
      ],
      ["branch", { type: "scalar", value: "dev" }],
    ],
  });
});
