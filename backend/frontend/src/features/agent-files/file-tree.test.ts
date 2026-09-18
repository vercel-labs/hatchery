import assert from "node:assert/strict";
import test from "node:test";

import { buildStorageFileTree } from "./file-tree.ts";

test("adds the default agent storage structure", () => {
  const tree = buildStorageFileTree([]);

  assert.deepEqual(
    tree.map((node) => [node.kind, node.name]),
    [
      ["directory", "memories"],
      ["directory", "schedules"],
      ["directory", "scripts"],
      ["directory", "skills"],
      ["file", "AGENTS.md"],
    ],
  );
  for (const directory of tree.filter((node) => node.kind === "directory")) {
    assert.deepEqual(directory.children, []);
  }
});

test("normalizes paths and lets real files replace default placeholders", () => {
  const tree = buildStorageFileTree([
    { path: "/skills/review/SKILL.md", size: 42 },
    { path: "AGENTS.md", size: 12 },
    { path: "memories\\today.md" },
    { path: "../outside.md" },
  ]);

  const agents = tree.find((node) => node.path === "AGENTS.md");
  const skills = tree.find((node) => node.path === "skills");
  const memories = tree.find((node) => node.path === "memories");

  assert.equal(agents?.kind === "file" ? agents.size : undefined, 12);
  assert.equal(skills?.kind === "directory" ? skills.children[0]?.path : undefined, "skills/review");
  assert.equal(memories?.kind === "directory" ? memories.children[0]?.path : undefined, "memories/today.md");
  assert.equal(tree.some((node) => node.name === "outside.md"), false);
});

test("can build only the supplied files", () => {
  assert.deepEqual(buildStorageFileTree([], false), []);
});
