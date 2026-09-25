import { Skeleton } from "@/components/ui/skeleton";
import { RepositoryPanelSkeleton } from "@/features/repository/repository-skeletons";
import { ConversationSkeleton } from "@/features/threads/thread-skeletons";

// Matches the AppShell layout while identity, agents, and chats load.
export function ConsoleLayoutSkeleton() {
  return (
    <div
      className="relative flex h-svh min-h-0 flex-col overflow-hidden [--navigation-width:16rem] md:grid md:grid-cols-[var(--navigation-width)_1px_minmax(0,1fr)] xl:[--navigation-width:17rem]"
      role="status"
      aria-label="Loading console"
    >
      <span className="sr-only">Loading console…</span>
      <aside className="relative z-20 flex min-h-0 shrink-0 flex-col border-b bg-muted/30 md:h-svh md:border-b-0">
        <header className="flex h-16 shrink-0 items-center justify-end px-5 md:hidden">
          <Skeleton className="size-8" />
        </header>
        <div className="hidden min-h-0 flex-1 flex-col md:flex">
          <div className="px-4 pt-3 pb-3">
            <div className="flex h-12 items-center gap-3 px-2">
              <Skeleton className="size-8 shrink-0 rounded-lg" />
              <div className="min-w-0 flex-1 space-y-2">
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
              </div>
              <Skeleton className="size-4 shrink-0" />
            </div>
          </div>
          <div className="flex shrink-0 flex-col gap-0.5 px-3 pb-4">
            <div className="flex h-9 items-center gap-2 px-3">
              <Skeleton className="size-4" />
              <Skeleton className="h-3.5 w-20" />
            </div>
            <div className="flex h-9 items-center gap-2 px-3">
              <Skeleton className="size-4" />
              <Skeleton className="h-3.5 w-24" />
            </div>
          </div>
          <section className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="flex h-9 shrink-0 items-center justify-between px-5 pb-2">
              <Skeleton className="h-3 w-16" />
              <Skeleton className="size-7 rounded-lg" />
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-hidden px-3 pb-4">
              {["w-3/5", "w-4/5", "w-2/3", "w-3/4"].map((width, index) => (
                <div
                  key={index}
                  className="flex min-h-16 shrink-0 items-center gap-3 rounded-xl px-3 py-3"
                >
                  <Skeleton className="size-7 shrink-0 rounded-lg" />
                  <div className="min-w-0 flex-1 space-y-2.5">
                    <Skeleton className={`h-3.5 ${width}`} />
                    <Skeleton className="h-3 w-1/2" />
                  </div>
                </div>
              ))}
            </div>
          </section>
          <footer className="shrink-0 border-t px-4 py-3">
            <div className="flex h-7.5 items-center px-1">
              <Skeleton className="h-3.5 w-4/5" />
            </div>
          </footer>
        </div>
      </aside>
      <div className="hidden bg-border md:block" />
      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_1px_48%]">
          <div className="flex min-h-0 min-w-0 flex-col">
            <header className="flex h-16 shrink-0 items-center border-b px-5">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="ml-auto h-3 w-20" />
            </header>
            <div className="min-h-0 flex-1 overflow-hidden">
              <div className="mx-auto w-full max-w-3xl px-5 py-8 sm:px-7">
                <ConversationSkeleton announce={false} />
              </div>
            </div>
            <div className="mx-auto w-full max-w-3xl shrink-0 p-4 pt-2 sm:px-6 sm:pb-5">
              <Skeleton className="h-24 w-full rounded-2xl" />
            </div>
          </div>
          <div className="hidden bg-border lg:block" />
          <aside className="hidden min-h-0 bg-muted/10 lg:flex lg:flex-col">
            <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
              <Skeleton className="h-8 w-20 rounded-lg" />
              <Skeleton className="h-8 w-16 rounded-lg" />
            </header>
            <RepositoryPanelSkeleton compact announce={false} />
          </aside>
        </div>
      </main>
    </div>
  );
}
