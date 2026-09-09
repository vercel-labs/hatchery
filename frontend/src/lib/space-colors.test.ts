import assert from "node:assert/strict";
import test from "node:test";

import {
  ACCENT_COLORS,
  ACCENT_FAMILIES,
  ACCENT_SHADES,
  accentColor,
  isAccentColor,
  normalizeAccentColor,
  resolveSpaceColor,
  resolveSpaceForeground,
  splitAccentColor,
} from "./space-colors.ts";

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

test("recognizes all twenty-eight explicit Geist accent IDs", () => {
  assert.deepEqual(ACCENT_COLORS, expectedColors);
  for (const color of expectedColors) assert.equal(isAccentColor(color), true);
  assert.equal(isAccentColor("blue"), false);
  assert.equal(isAccentColor("blue-500"), false);
  assert.equal(isAccentColor("#38bdf8"), false);
});

test("normalizes stored family aliases to 700", () => {
  for (const family of ACCENT_FAMILIES) {
    assert.equal(normalizeAccentColor(family), `${family}-700`);
  }
  assert.equal(normalizeAccentColor("purple-900"), "purple-900");
  assert.equal(normalizeAccentColor("#38bdf8"), null);
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

test("resolves explicit IDs, aliases, foregrounds, and legacy colors", () => {
  assert.equal(resolveSpaceColor("purple-600"), "var(--geist-purple-600)");
  assert.equal(resolveSpaceColor("purple-800"), "var(--geist-purple-800)");
  assert.equal(resolveSpaceColor("purple-900"), "var(--geist-purple-900)");
  assert.equal(resolveSpaceColor("purple"), "var(--geist-purple-700)");
  assert.equal(resolveSpaceForeground("purple"), "var(--geist-purple-1000)");
  assert.equal(resolveSpaceColor("#38bdf8"), "#38bdf8");
  assert.equal(resolveSpaceColor("oklch(50% 0.2 200)"), "oklch(50% 0.2 200)");
  assert.equal(resolveSpaceColor(undefined), "var(--muted-foreground)");
});
