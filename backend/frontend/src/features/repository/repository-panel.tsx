import { useMemo, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { GitBranch, RefreshCw } from "lucide-react";
import type { Repository, RepositoryFile } from "@/lib/api-types";
import { useApi } from "@/hooks/use-api";
import { FileTree, fileTree } from "./file-tree";
import { FilePreview } from "./file-preview";
import { ChangesList } from "./changes-list";
import {
  FilePreviewSkeleton,
  RepositoryPanelSkeleton,
} from "./repository-skeletons";

type Props = {
  prefix?: string;
  chatId?: string;
  proposal?: string;
  // A thread's checkpoint: its changes refresh when the thread reports a new one.
  revision?: string;
  onApprove?: (sha: string) => Promise<void>;
  reviewUrl?: string;
  compact?: boolean;
  loadingFallback?: ReactNode;
  // Replaces the branch label at the top of the file tree, e.g. a version switcher.
  branch?: ReactNode;
};
export default function RepositoryPanel({
  prefix = "",
  chatId,
  proposal,
  revision,
  onApprove,
  branch,
  reviewUrl,
  compact = false,
  loadingFallback,
}: Props) {
  const [chosen, setChosen] = useState("");
  const [comparison, setComparison] = useState<"full" | "latest">("full");
  const [mode, setMode] = useState<"file" | "diff">(chatId ? "diff" : "file");
  const [changesOnly, setChangesOnly] = useState(Boolean(chatId));
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState("");
  const params = new URLSearchParams();
  if (chatId) params.set("chat_id", chatId);
  if (proposal) params.set("proposal", proposal);
  if (chatId && !proposal) params.set("comparison", comparison);
  if (revision) params.set("revision", revision);
  const path = `/api/repository?${params}`;
  const {
    data,
    error: loadError,
    mutate,
    isValidating,
  } = useApi<Repository>(path, revision ? 0 : 5_000);
  const changes = useMemo(
    () =>
      new Map(
        data?.changes.map((change) => [change.path, change.status]) ?? [],
      ),
    [data?.changes],
  );
  const paths = useMemo(() => {
    const all = changesOnly
      ? Array.from(changes.keys())
      : Array.from(
          new Set([
            ...(data?.files.map((file) => file.path) ?? []),
            ...changes.keys(),
          ]),
        );
    return all.filter((path) => path.startsWith(prefix)).sort();
  }, [data?.files, changes, changesOnly, prefix]);
  const tree = useMemo(() => fileTree(paths, changes), [paths, changes]);
  const selected = paths.includes(chosen)
    ? chosen
    : (paths.find((p) => p.endsWith("/AGENTS.md")) ?? paths[0] ?? "");
  const fileParams = new URLSearchParams(params);
  fileParams.set("path", selected);
  if (data) fileParams.set("revision", data.sha);
  const file = useApi<RepositoryFile>(
    !compact && selected && data ? `/api/repository?${fileParams}` : null,
  );
  const pending = Boolean(proposal && data && !data.merged);

  async function approve() {
    if (!onApprove || approving || !data) return;
    setApproving(true);
    setError("");
    try {
      await onApprove(data.sha);
      await mutate();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Approval failed");
    } finally {
      setApproving(false);
    }
  }

  if (!data && loadError)
    return (
      <div className="p-8 text-sm text-destructive" role="alert">
        {loadError.message}
      </div>
    );
  if (!data)
    return (
      loadingFallback ?? (
        <RepositoryPanelSkeleton
          compact={compact}
          showModeToggle={Boolean(chatId && !compact)}
        />
      )
    );
  const count = data.changes.filter((change) =>
    change.path.startsWith(prefix),
  ).length;
  const label = (
    <span
      className="min-w-0 truncate"
      title={
        proposal
          ? data.summary
          : chatId
            ? comparison === "full"
              ? "Changes since this thread started. Updates after tool checkpoints."
              : "Thread changes after its latest sync with main."
            : data.branch
      }
    >
      {proposal
        ? data.merged
          ? "Merged into main"
          : "Awaiting review"
        : chatId
          ? `${count} changed ${count === 1 ? "file" : "files"}`
          : "main"}
    </span>
  );
  const review =
    pending && onApprove ? (
      <Button
        size={compact ? "sm" : "xs"}
        className="ml-auto"
        disabled={approving}
        onClick={() => void approve()}
      >
        {approving ? "Merging…" : "Approve & merge"}
      </Button>
    ) : pending && reviewUrl ? (
      <a
        className="ml-auto underline"
        href={reviewUrl}
        target="_blank"
        rel="noreferrer"
      >
        Review on GitHub ↗
      </a>
    ) : null;
  const refresh = (
    <Button
      className={
        compact && (pending || (chatId && !proposal)) ? "" : "ml-auto"
      }
      size={compact ? "icon-sm" : "icon-xs"}
      variant="ghost"
      aria-label="Refresh files"
      title="Refresh files"
      disabled={isValidating}
      onClick={() => void mutate()}
    >
      <RefreshCw className={isValidating ? "animate-spin" : ""} />
    </Button>
  );
  const alert =
    error || loadError ? (
      <p role="alert" className="text-xs text-destructive">
        {error || loadError.message}
      </p>
    ) : null;
  return (
    <section
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      aria-label="Repository browser"
    >
      {compact ? (
        <div className="shrink-0 space-y-2 border-b px-4 py-2.5">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <GitBranch className="size-3.5" aria-hidden />
            {label}
            {review}
            {chatId && !proposal ? (
              <div
                role="group"
                aria-label="Diff comparison"
                className="ml-auto flex gap-1"
              >
                <Button
                  size="xs"
                  variant={comparison === "full" ? "secondary" : "ghost"}
                  aria-label="Full diff"
                  aria-pressed={comparison === "full"}
                  title="Compare with the main commit where this thread started"
                  onClick={() => setComparison("full")}
                >
                  Full
                </Button>
                <Button
                  size="xs"
                  variant={comparison === "latest" ? "secondary" : "ghost"}
                  aria-label="Latest diff"
                  aria-pressed={comparison === "latest"}
                  title="Compare with the latest main included in this thread"
                  onClick={() => setComparison("latest")}
                >
                  Latest
                </Button>
              </div>
            ) : null}
            {refresh}
          </div>
          {alert}
        </div>
      ) : null}
      {compact && paths.length ? (
        <ChangesList
          paths={paths}
          query={new URLSearchParams({
            ...Object.fromEntries(params),
            revision: data.sha,
          }).toString()}
        />
      ) : (
        <div
          className={
            compact
              ? "flex min-h-0 min-w-0 flex-1 flex-col"
              : "grid min-h-0 min-w-0 flex-1 grid-cols-1 grid-rows-[minmax(0,0.45fr)_minmax(0,1fr)] md:grid-cols-[240px_minmax(0,1fr)] md:grid-rows-[minmax(0,1fr)]"
          }
        >
          {!compact ? (
            <aside
              className="flex min-h-0 min-w-0 flex-col border-b md:border-r md:border-b-0"
              aria-label="File tree"
            >
              <div className="shrink-0 space-y-1.5 px-3 pt-2 text-xs text-muted-foreground">
                <div className="flex h-7 items-center gap-1.5">
                  {branch ?? (
                    <>
                      <GitBranch className="size-3.5 shrink-0" aria-hidden />
                      {label}
                    </>
                  )}
                  {refresh}
                </div>
                {proposal && (branch || review) ? (
                  <div className="flex items-center gap-2">
                    {branch ? label : null}
                    {review}
                  </div>
                ) : null}
                {chatId ? (
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={changesOnly}
                      onChange={(event) => setChangesOnly(event.target.checked)}
                    />
                    Changed files only
                  </label>
                ) : null}
                {alert}
              </div>
              <div
                className="min-h-0 flex-1 overflow-auto overscroll-contain p-3 [scrollbar-gutter:stable] [scrollbar-width:thin]"
                role="region"
                aria-label="File list"
                tabIndex={0}
              >
                <FileTree
                  node={tree}
                  selected={selected}
                  onSelect={setChosen}
                />
                {!paths.length ? (
                  <p className="p-3 text-xs text-muted-foreground">
                    {changesOnly
                      ? "No checkpointed changes yet."
                      : "No files in this section yet."}
                  </p>
                ) : null}
              </div>
            </aside>
          ) : null}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {selected ? (
              <>
                <header className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
                  <code
                    className="min-w-0 flex-1 truncate text-xs"
                    title={selected}
                  >
                    {selected}
                  </code>
                  {chatId ? (
                    <div
                      role="group"
                      aria-label="Preview mode"
                      className="flex gap-1"
                    >
                      <Button
                        size="sm"
                        variant={mode === "file" ? "secondary" : "ghost"}
                        aria-pressed={mode === "file"}
                        onClick={() => setMode("file")}
                      >
                        File
                      </Button>
                      <Button
                        size="sm"
                        variant={mode === "diff" ? "secondary" : "ghost"}
                        aria-pressed={mode === "diff"}
                        onClick={() => setMode("diff")}
                      >
                        Diff
                      </Button>
                    </div>
                  ) : null}
                </header>
                <div
                  className="min-h-0 flex-1 overflow-auto overscroll-contain [scrollbar-gutter:stable] [scrollbar-width:thin]"
                  role="region"
                  aria-label="File preview"
                  tabIndex={0}
                >
                  {file.error ? (
                    <p role="alert" className="p-5 text-sm text-destructive">
                      {file.error.message}
                    </p>
                  ) : file.data ? (
                    <FilePreview data={file.data} mode={mode} />
                  ) : (
                    <FilePreviewSkeleton />
                  )}
                </div>
              </>
            ) : (
              <div className="grid min-h-0 flex-1 place-content-center gap-2 overflow-auto p-8 text-center">
                <h3 className="text-sm font-medium">
                  {changesOnly ? "No changes yet" : "Select a file"}
                </h3>
                <p className="max-w-sm text-xs leading-5 text-muted-foreground">
                  {changesOnly
                    ? compact
                      ? "File changes appear here after each checkpoint."
                      : "Choose all files to explore this thread’s workspace."
                    : "Choose a file from the repository."}
                </p>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
