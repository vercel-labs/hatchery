// Agents often draw boards, grids, and diagrams inline without a code fence.
// CommonMark strips per-line indentation and turns trailing spaces into hard
// breaks, which destroys that alignment. Wrapping such runs in a fence before
// parsing renders them verbatim while the surrounding prose stays Markdown.

const fenceOpen = /^ {0,3}(`{3,}|~{3,})/;
const gridChar = /[|+\u2500-\u257f]/;
const tableDelimiter = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/;
const tokenSeparator = /[\s|+\-=\u2500-\u257f]+/;

function tokens(line: string): string[] {
  return line.split(tokenSeparator).filter(Boolean);
}

function isGridLine(line: string): boolean {
  return (
    gridChar.test(line) && tokens(line).every((token) => token.length <= 3)
  );
}

// A row of short column labels, such as "  1   2   3", adjacent to a grid.
function isLabelLine(line: string): boolean {
  const parts = tokens(line);
  return (
    parts.length >= 2 &&
    parts.every((token) => token.length <= 3 && !token.includes("`"))
  );
}

export function fencePreformattedBlocks(text: string): string {
  const lines = text.split("\n");
  const out: string[] = [];
  let fence: string | undefined;
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const opening = line.match(fenceOpen)?.[1];
    if (fence) {
      if (opening && opening[0] === fence[0] && opening.length >= fence.length)
        fence = undefined;
      out.push(line);
      index += 1;
      continue;
    }
    if (opening) {
      fence = opening;
      out.push(line);
      index += 1;
      continue;
    }
    let end = index;
    while (end < lines.length && !lines[end].match(fenceOpen)) {
      if (isGridLine(lines[end]) || isLabelLine(lines[end])) end += 1;
      else break;
    }
    const block = lines.slice(index, end);
    const grids = block.filter(isGridLine).length;
    if (grids < 2 || block.some((row) => tableDelimiter.test(row))) {
      out.push(line);
      index += 1;
      continue;
    }
    out.push("```", ...block, "```");
    index = end;
  }
  return out.join("\n");
}
