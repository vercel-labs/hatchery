import assert from "node:assert/strict";
import test from "node:test";

import {
  ACCENT_COLORS,
  isAccentColor,
  resolveSpaceColor,
} from "./space-colors.ts";

test("recognizes the seven supported accent families", () => {
  assert.deepEqual(ACCENT_COLORS, [
    "blue",
    "red",
    "amber",
    "green",
    "teal",
    "purple",
    "pink",
  ]);
  for (const color of ACCENT_COLORS) assert.equal(isAccentColor(color), true);
  assert.equal(isAccentColor("#38bdf8"), false);
});

test("resolves semantic colors and passes legacy colors through", () => {
  assert.equal(resolveSpaceColor("purple"), "var(--space-accent-purple)");
  assert.equal(resolveSpaceColor("#38bdf8"), "#38bdf8");
  assert.equal(resolveSpaceColor("oklch(50% 0.2 200)"), "oklch(50% 0.2 200)");
  assert.equal(resolveSpaceColor(undefined), "var(--muted-foreground)");
});
