import assert from "node:assert/strict";
import test from "node:test";

import {
  ACCENT_COLORS,
  ACCENT_FAMILIES,
  ACCENT_SHADES,
  accentColor,
  resolveAgentColor,
  resolveAgentForeground,
  splitAccentColor,
} from "./agent-colors.ts";

const expectedColors = [
  "blue-600",
  "blue-700",
  "blue-800",
  "blue-900",
  "red-600",
  "red-700",
  "red-800",
  "red-900",
  "amber-600",
  "amber-700",
  "amber-800",
  "amber-900",
  "green-600",
  "green-700",
  "green-800",
  "green-900",
  "teal-600",
  "teal-700",
  "teal-800",
  "teal-900",
  "purple-600",
  "purple-700",
  "purple-800",
  "purple-900",
  "pink-600",
  "pink-700",
  "pink-800",
  "pink-900",
];

test("lists all twenty-eight Geist accent IDs", () => {
  assert.deepEqual(ACCENT_COLORS, expectedColors);
});

test("builds and splits only complete picker values", () => {
  for (const family of ACCENT_FAMILIES) {
    for (const shade of ACCENT_SHADES) {
      const color = accentColor(family, shade);
      assert.deepEqual(splitAccentColor(color), { family, shade });
    }
  }
  assert.equal(splitAccentColor(null), null);
});

test("resolves accent IDs and foregrounds to Geist variables", () => {
  assert.equal(resolveAgentColor("purple-600"), "var(--geist-purple-600)");
  assert.equal(resolveAgentColor("purple-900"), "var(--geist-purple-900)");
  assert.equal(resolveAgentForeground("purple"), "var(--geist-purple-1000)");
  assert.equal(resolveAgentColor(undefined), "var(--muted-foreground)");
});
