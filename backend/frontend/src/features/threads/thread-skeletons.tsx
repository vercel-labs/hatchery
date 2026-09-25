import { Skeleton } from "@/components/ui/skeleton";

export function ConversationSkeleton({
  announce = true,
}: {
  announce?: boolean;
}) {
  return (
    <div
      className="flex flex-col gap-6"
      role={announce ? "status" : undefined}
      aria-label={announce ? "Loading conversation" : undefined}
    >
      {announce ? <span className="sr-only">Loading conversation…</span> : null}
      <div className="relative min-w-0 space-y-3">
        <div className="mb-1.5 flex h-4 items-center">
          <Skeleton className="h-2.5 w-14" />
        </div>
        <div className="space-y-2.5">
          <div className="flex h-7 items-center">
            <Skeleton className="h-3.5 w-11/12" />
          </div>
          <div className="flex h-7 items-center">
            <Skeleton className="h-3.5 w-4/5" />
          </div>
          <div className="flex h-7 items-center">
            <Skeleton className="h-3.5 w-3/5" />
          </div>
        </div>
      </div>
      <div className="relative min-w-0 w-4/5 self-end">
        <div className="mb-1.5 flex h-4 items-center justify-end pr-1">
          <Skeleton className="h-2.5 w-12" />
        </div>
        <div className="space-y-3 rounded-2xl bg-muted/70 px-4 py-3">
          <div className="flex h-7 items-center justify-end">
            <Skeleton className="h-3.5 w-11/12 bg-muted-foreground/15" />
          </div>
          <div className="flex h-7 items-center justify-end">
            <Skeleton className="h-3.5 w-2/3 bg-muted-foreground/15" />
          </div>
        </div>
      </div>
      <div className="relative min-w-0 space-y-3">
        <div className="mb-1.5 flex h-4 items-center">
          <Skeleton className="h-2.5 w-16" />
        </div>
        <div className="space-y-2.5">
          <div className="flex h-7 items-center">
            <Skeleton className="h-3.5 w-5/6" />
          </div>
          <div className="flex h-7 items-center">
            <Skeleton className="h-3.5 w-full" />
          </div>
          <div className="flex h-7 items-center">
            <Skeleton className="h-3.5 w-7/12" />
          </div>
        </div>
      </div>
    </div>
  );
}

export function ThreadStateSkeleton() {
  return (
    <div
      className="min-h-0 flex-1 space-y-6 p-5"
      role="status"
      aria-label="Loading thread state"
    >
      <span className="sr-only">Loading thread state…</span>
      <div className="flex items-center gap-2">
        <Skeleton className="size-7 rounded-lg" />
        <Skeleton className="h-4 w-32" />
      </div>
      <div className="space-y-2.5">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-20 w-full rounded-lg" />
      </div>
      <div className="space-y-3 border-y py-4">
        <div className="flex justify-between gap-4">
          <Skeleton className="h-3.5 w-20" />
          <Skeleton className="h-5 w-7" />
        </div>
        <Skeleton className="h-3 w-36" />
      </div>
      <div className="space-y-3">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-3/4" />
      </div>
    </div>
  );
}
