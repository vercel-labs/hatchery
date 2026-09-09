import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const globals = readFileSync(new URL("./globals.css", import.meta.url), "utf8");
const typeset = readFileSync(new URL("./typeset.css", import.meta.url), "utf8");
const shell = readFileSync(new URL("./(app)/app-shell.tsx", import.meta.url), "utf8");
const terminal = readFileSync(
  new URL("../components/terminal-pane.tsx", import.meta.url),
  "utf8",
);
const picker = readFileSync(
  new URL("../components/space-color-picker.tsx", import.meta.url),
  "utf8",
);

test("defines theme-aware Geist accent scales", () => {
  assert.match(globals, /Geist color scales: https:\/\/vercel\.com\/geist\/colors\.md/);
  for (const family of ["blue", "red", "amber", "green", "teal", "purple", "pink"]) {
    for (const shade of ["600", "700", "800", "900", "1000"]) {
      assert.equal(globals.match(new RegExp(`--geist-${family}-${shade}:`, "g"))?.length, 2);
    }
  }
  assert.match(globals, /--geist-blue-600: oklch\(73\.08% 0\.1583 248\.133320980386\)/);
  assert.match(globals, /--geist-blue-800: oklch\(51\.51% 0\.2399 257\.85\)/);
  assert.match(globals, /--geist-blue-900: oklch\(53\.18% 0\.2399 256\.99\)/);
  assert.match(globals, /--geist-blue-600: oklch\(64\.94% 0\.1982 251\.8131841760864\)/);
  assert.match(globals, /--geist-blue-800: oklch\(51\.51% 0\.2307 257\.85\)/);
  assert.match(globals, /--geist-blue-900: oklch\(71\.7% 0\.1648 250\.794\)/);
  assert.match(globals, /--geist-pink-1000: oklch\(26% 0\.0977 359\)/);
  assert.match(globals, /--geist-pink-1000: oklch\(95\.74% 0\.0326 350\.08\)/);
});

test("renders one button grid without toggle controls", () => {
  assert.match(picker, /grid-cols-4/);
  assert.match(picker, /ACCENT_FAMILIES\.map/);
  assert.match(picker, /ACCENT_SHADES\.map/);
  assert.doesNotMatch(picker, /ToggleGroup/);
});

test("uses semantic Geist status, destructive, sidebar, and mark colors", () => {
  assert.match(shell, /bg-status-green-700/);
  assert.match(shell, /bg-status-amber-700/);
  assert.match(terminal, /bg-status-green-700/);
  assert.match(terminal, /bg-status-amber-700/);
  assert.match(terminal, /bg-status-red-700/);
  assert.match(terminal, /background: "#0a0a0a"/);
  assert.equal(globals.match(/--destructive: var\(--geist-red-900\)/g)?.length, 2);
  assert.match(globals, /--sidebar-primary: var\(--primary\)/);
  assert.match(typeset, /background-color: var\(--markdown-mark\)/);
  assert.doesNotMatch(typeset, /oklch\(0\.86 0\.17 95\)/);
});
