import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const codeLineWidths = ["w-2/3", "w-5/6", "w-1/2", "w-3/4", "w-7/12", "w-4/5"];

function CodeLines({ count = 6 }: { count?: number }) {
  return (
    <div className="space-y-3 p-5">
      {codeLineWidths.slice(0, count).map((width, index) => (
        <div key={index} className="flex items-center gap-4">
          <Skeleton className="h-3 w-7 shrink-0" />
          <Skeleton className={cn("h-3", width)} />
        </div>
      ))}
    </div>
  );
}

export function FilePreviewSkeleton({
  label = "Loading file preview",
  className,
}: {
  label?: string;
  className?: string;
}) {
  return (
    <div className={cn("min-h-32", className)} role="status" aria-label={label}>
      <span className="sr-only">{label}…</span>
      <CodeLines />
    </div>
  );
}

function ChangedFileSkeleton({ index }: { index: number }) {
  return (
    <section className="border-b">
      <div className="flex min-h-11 items-center gap-2 border-b px-3 py-2">
        <Skeleton className="size-3.5 shrink-0" />
        <div className="flex h-5 flex-1 items-center">
          <Skeleton
            className={cn(
              "h-3",
              index === 0 ? "w-2/3" : index === 1 ? "w-1/2" : "w-3/5",
            )}
          />
        </div>
        <div className="flex h-5 w-12 shrink-0 items-center justify-end">
          <Skeleton className="h-3 w-10" />
        </div>
        <Skeleton className="h-7 w-20 shrink-0 rounded-lg" />
      </div>
      <CodeLines count={index === 1 ? 4 : 5} />
    </section>
  );
}

export function RepositoryPanelSkeleton({
  compact = false,
  showModeToggle = false,
  announce = true,
}: {
  compact?: boolean;
  showModeToggle?: boolean;
  announce?: boolean;
}) {
  return (
    <section
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      role={announce ? "status" : undefined}
      aria-label={
        announce
          ? compact
            ? "Loading changes"
            : "Loading repository"
          : undefined
      }
    >
      {announce ? (
        <span className="sr-only">
          {compact ? "Loading changes…" : "Loading repository…"}
        </span>
      ) : null}
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2.5">
        <Skeleton className="size-3.5" />
        <Skeleton className="h-3 w-28" />
        <Skeleton className="ml-auto size-7 rounded-lg" />
      </div>
      {compact ? (
        <div className="min-h-0 flex-1 overflow-hidden">
          {[0, 1, 2].map((index) => (
            <ChangedFileSkeleton key={index} index={index} />
          ))}
        </div>
      ) : (
        <>
          <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(0,0.45fr)_minmax(0,1fr)] md:grid-cols-[240px_minmax(0,1fr)] md:grid-rows-[minmax(0,1fr)]">
            <aside className="flex min-h-0 flex-col border-b md:border-r md:border-b-0">
              <div className="border-b p-3">
                <Skeleton className="h-8 w-full rounded-lg" />
              </div>
              <div className="space-y-4 p-4">
                <Skeleton className="h-3 w-24" />
                <Skeleton className="ml-3 h-3 w-32" />
                <Skeleton className="ml-3 h-3 w-24" />
                <Skeleton className="h-3 w-28" />
                <Skeleton className="ml-3 h-3 w-36" />
                <Skeleton className="ml-6 h-3 w-28" />
              </div>
            </aside>
            <div className="flex min-h-0 flex-col">
              <div
                className={cn(
                  "flex items-center gap-1 border-b px-4",
                  showModeToggle ? "py-2" : "py-2.5",
                )}
              >
                <Skeleton className="h-3 w-56 max-w-[70%]" />
                {showModeToggle ? (
                  <div className="ml-auto flex gap-1">
                    <Skeleton className="h-7 w-12 rounded-lg" />
                    <Skeleton className="h-7 w-12 rounded-lg" />
                  </div>
                ) : null}
              </div>
              <CodeLines />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3 border-t px-4 py-2">
            <Skeleton className="h-2.5 w-2/5" />
            <Skeleton className="ml-auto h-2.5 w-16" />
          </div>
        </>
      )}
    </section>
  );
}
