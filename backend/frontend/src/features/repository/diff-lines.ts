export type DiffLine = {
  text: string;
  kind: "addition" | "deletion" | "context" | "hunk" | "meta";
  before?: number;
  after?: number;
};

export function diffLines(diff: string): DiffLine[] {
  let before: number | undefined;
  let after: number | undefined;
  const lines: DiffLine[] = [];
  for (const text of diff.split("\n")) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (hunk) {
      before = Number(hunk[1]);
      after = Number(hunk[2]);
      lines.push({ text, kind: "hunk" });
    } else if (
      before === undefined &&
      /^(diff --git |index |--- |\+\+\+ )/.test(text)
    ) {
      continue;
    } else if (text.startsWith("+")) {
      lines.push({ text, kind: "addition", after });
      if (after !== undefined) after++;
    } else if (text.startsWith("-")) {
      lines.push({ text, kind: "deletion", before });
      if (before !== undefined) before++;
    } else if (
      text.startsWith(" ") &&
      before !== undefined &&
      after !== undefined
    ) {
      lines.push({ text, kind: "context", before: before++, after: after++ });
    } else if (text) {
      lines.push({ text, kind: "meta" });
    }
  }
  return lines;
}
