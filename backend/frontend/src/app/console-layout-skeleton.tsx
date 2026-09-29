import { Skeleton } from "@/components/ui/skeleton";
import { ConversationSkeleton } from "@/features/threads/thread-skeletons";

// Matches the AppShell layout while identity, agents, and chats load: the
// sidebar (agent selector, chat list, profile) and the tabbed main pane.
export function ConsoleLayoutSkeleton() {
  return (
    <div
      className="relative flex h-svh min-h-0 flex-col overflow-hidden [--navigation-width:16rem] md:grid md:grid-cols-[var(--navigation-width)_1px_minmax(0,1fr)]"
      role="status"
      aria-label="Loading console"
    >
      <span className="sr-only">Loading console…</span>
      <aside className="relative z-20 flex min-h-0 shrink-0 flex-col border-b bg-muted/30 md:h-svh md:border-b-0">
        <header className="flex h-12 shrink-0 items-center justify-end px-5 md:hidden">
          <Skeleton className="size-8" />
        </header>
        <div className="hidden min-h-0 flex-1 flex-col md:flex">
          <div className="flex flex-col gap-2 p-2">
            <div className="flex h-12 items-center gap-3 px-2">
              <Skeleton className="size-8 shrink-0 rounded-lg" />
              <div className="min-w-0 flex-1 space-y-2">
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
              </div>
              <Skeleton className="size-4 shrink-0" />
            </div>
            <Skeleton className="h-8 w-full rounded-lg" />
          </div>
          <section className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="flex h-10 shrink-0 items-center gap-1 px-3 pb-2">
              <Skeleton className="h-8 flex-1 rounded-lg" />
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
          <footer className="shrink-0 border-t p-2">
            <div className="flex h-11 items-center gap-3 px-2">
              <Skeleton className="size-6 shrink-0 rounded-full" />
              <Skeleton className="h-3.5 w-1/2" />
            </div>
          </footer>
        </div>
      </aside>
      <div className="hidden bg-border md:block" />
      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
          <Skeleton className="size-7 rounded-lg" />
          {["w-12", "w-16", "w-20", "w-12"].map((width, index) => (
            <Skeleton key={index} className={`h-7 ${width} rounded-lg`} />
          ))}
        </header>
        <header className="flex h-12 shrink-0 items-center border-b px-4">
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
      </main>
    </div>
  );
}
