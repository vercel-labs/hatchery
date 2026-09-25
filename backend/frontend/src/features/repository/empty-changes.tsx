import { GitBranch } from "lucide-react";

export function EmptyChanges() {
  return (
    <section
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      aria-label="Repository browser"
    >
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2.5 text-xs text-muted-foreground">
        <GitBranch className="size-3.5" aria-hidden />
        <span className="flex h-7 items-center">0 changed files</span>
      </div>
      <div className="grid min-h-0 flex-1 place-content-center gap-2 overflow-auto p-8 text-center">
        <h3 className="text-sm font-medium">No changes yet</h3>
        <p className="max-w-sm text-xs leading-5 text-muted-foreground">
          File changes appear here after each checkpoint.
        </p>
      </div>
    </section>
  );
}
