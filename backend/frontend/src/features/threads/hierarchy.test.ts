import assert from "node:assert/strict";
import test from "node:test";

import { buildHierarchy, type HierarchyNode } from "./hierarchy.ts";

type Item = { id: string; parentId?: string | null; title: string };

function ids(nodes: HierarchyNode<Item>[]): unknown[] {
  return nodes.map((node) => [node.item.id, ids(node.children)]);
}

test("builds a stable hierarchy in input order", () => {
  const tree = buildHierarchy<Item>([
    { id: "root", title: "Root" },
    { id: "child", parentId: "root", title: "Child" },
    { id: "sibling", parentId: "root", title: "Sibling" },
    { id: "leaf", parentId: "child", title: "Leaf" },
  ]);

  assert.deepEqual(ids(tree), [
    ["root", [["child", [["leaf", []]]], ["sibling", []]]],
  ]);
});

test("promotes orphans to roots and marks them", () => {
  const tree = buildHierarchy<Item>([
    { id: "orphan", parentId: "missing", title: "Orphan" },
  ]);

  assert.equal(tree[0]?.item.id, "orphan");
  assert.equal(tree[0]?.orphaned, true);
});

test("breaks cycles without dropping nodes or descendants", () => {
  const tree = buildHierarchy<Item>([
    { id: "a", parentId: "b", title: "A" },
    { id: "b", parentId: "c", title: "B" },
    { id: "c", parentId: "a", title: "C" },
    { id: "child", parentId: "a", title: "Child" },
  ]);

  assert.deepEqual(tree.map((node) => node.item.id), ["a", "b", "c"]);
  assert.equal(tree.every((node) => node.cyclic), true);
  assert.equal(tree[0]?.children[0]?.item.id, "child");
});

test("keeps the first item when ids are duplicated", () => {
  const tree = buildHierarchy<Item>([
    { id: "same", title: "First" },
    { id: "same", title: "Second" },
  ]);

  assert.equal(tree.length, 1);
  assert.equal(tree[0]?.item.title, "First");
});
