import { cn } from "@/lib/utils";

type Branch = "line" | "branch" | "end" | null;

// Work from the visible preorder so collapse, search, and promoted roots share the same guides.
export function threadTreeBranches(rows: { depth: number }[]): Branch[][] {
  const nextDepths: number[] = [];
  const lastSibling: boolean[] = [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const depth = rows[index].depth;
    while (nextDepths.length && nextDepths[nextDepths.length - 1] > depth)
      nextDepths.pop();
    lastSibling[index] = nextDepths[nextDepths.length - 1] !== depth;
    if (lastSibling[index]) nextDepths.push(depth);
  }
  const continuing: boolean[] = [];
  return rows.map(({ depth }, index) => {
    continuing.length = depth;
    continuing[depth] = !lastSibling[index];
    const visibleDepth = Math.min(depth, 6);
    return Array.from({ length: visibleDepth }, (_, level) =>
      level === visibleDepth - 1
        ? lastSibling[index]
          ? "end"
          : "branch"
        : continuing[level + 1]
          ? "line"
          : null,
    );
  });
}

export function ThreadTreeGuides({ branches }: { branches: Branch[] }) {
  return (
    <span
      aria-hidden
      className="pointer-events-none absolute inset-y-0 start-[calc(0.75rem+1px)] z-10 flex"
    >
      {branches.map((branch, level) => (
        <span key={level} className="relative w-3 shrink-0">
          {branch ? (
            <span
              className={cn(
                "absolute -top-1 start-0 border-s border-border",
                branch === "end" ? "bottom-1/2" : "bottom-0",
              )}
            />
          ) : null}
          {branch === "branch" || branch === "end" ? (
            <span className="absolute start-0 top-1/2 w-2 border-t border-border" />
          ) : null}
        </span>
      ))}
    </span>
  );
}
