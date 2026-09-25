import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useApi } from "@/hooks/use-api";
import type { RepositoryFile } from "@/lib/api-types";
import { diffLines } from "./diff-lines";
import { FilePreview } from "./file-preview";
import { FilePreviewSkeleton } from "./repository-skeletons";

type FileProps = {
  path: string;
  query: string;
};

function ChangedFile({ path, query }: FileProps) {
  const [expanded, setExpanded] = useState(true);
  const [mode, setMode] = useState<"diff" | "file">("diff");
  const [nearby, setNearby] = useState(
    () => typeof IntersectionObserver === "undefined",
  );
  const section = useRef<HTMLElement>(null);
  const previewId = useId();

  // Load ahead of scrolling without requesting every file in a large review at once.
  useEffect(() => {
    if (nearby || !section.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNearby(true);
          observer.disconnect();
        }
      },
      { rootMargin: "600px 0px" },
    );
    observer.observe(section.current);
    return () => observer.disconnect();
  }, [nearby]);

  const params = new URLSearchParams(query);
  params.set("path", path);
  const file = useApi<RepositoryFile>(
    nearby ? `/api/repository?${params}` : null,
    0,
    { keepPreviousData: true },
  );
  const diff = file.data?.diff;
  const stats = useMemo(() => {
    if (!diff) return null;
    const lines = diffLines(diff);
    return {
      added: lines.filter((line) => line.kind === "addition").length,
      deleted: lines.filter((line) => line.kind === "deletion").length,
    };
  }, [diff]);

  return (
    <section
      ref={section}
      aria-label={`Changes to ${path}`}
      className="relative min-w-0 border-b"
    >
      <header className="sticky top-0 z-10 flex min-h-11 min-w-0 items-center gap-2 border-b bg-background px-3 py-2">
        <h4 className="min-w-0 flex-1">
          <button
            className="flex w-full items-center gap-2 rounded text-left focus-visible:outline-2 focus-visible:outline-ring"
            aria-expanded={expanded}
            aria-controls={previewId}
            title={path}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? (
              <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
            )}
            <code className="min-w-0 truncate text-xs leading-5">{path}</code>
          </button>
        </h4>
        <span className="flex h-5 w-12 shrink-0 items-center justify-end">
          {stats ? (
            <span
              className="flex gap-1.5 font-mono text-[11px]"
              title={
                file.data?.diff_truncated
                  ? "Counts for the displayed portion"
                  : undefined
              }
            >
              <span
                className="text-green-700 dark:text-green-400"
                aria-label={`${stats.added} additions`}
              >
                +{stats.added}
              </span>
              <span
                className="text-red-700 dark:text-red-400"
                aria-label={`${stats.deleted} deletions`}
              >
                −{stats.deleted}
              </span>
            </span>
          ) : !file.data && !file.error ? (
            <Skeleton className="h-3 w-10" />
          ) : null}
        </span>
        {expanded ? (
          <span
            className="flex h-7 w-20 shrink-0 items-center"
            aria-hidden={
              file.data?.after || mode === "file" ? undefined : "true"
            }
          >
            {file.data?.after || mode === "file" ? (
              <Button
                size="sm"
                variant="ghost"
                className="w-20 text-xs text-muted-foreground"
                aria-label={`${mode === "diff" ? "Show full file" : "Show diff"}: ${path}`}
                onClick={() =>
                  setMode((value) => (value === "diff" ? "file" : "diff"))
                }
              >
                {mode === "diff" ? "Full file" : "Show diff"}
              </Button>
            ) : !file.data && !file.error ? (
              <Skeleton className="h-7 w-20 rounded-lg" />
            ) : null}
          </span>
        ) : null}
      </header>
      <div
        id={previewId}
        hidden={!expanded}
        className="min-w-0 overflow-x-auto [scrollbar-width:thin]"
      >
        {file.error ? (
          <div className="flex items-center gap-3 p-5">
            <p role="alert" className="flex-1 text-xs text-destructive">
              {file.error.message}
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void file.mutate()}
              aria-label={`Retry ${path}`}
            >
              Retry
            </Button>
          </div>
        ) : file.data ? (
          <FilePreview data={file.data} mode={mode} />
        ) : (
          <FilePreviewSkeleton label={`Loading ${path}`} />
        )}
      </div>
    </section>
  );
}

export function ChangesList({
  paths,
  query,
}: {
  paths: string[];
  query: string;
}) {
  return (
    <div
      className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain [scrollbar-width:thin]"
      role="region"
      aria-label="File changes"
      tabIndex={0}
    >
      {paths.map((path) => (
        <ChangedFile key={path} path={path} query={query} />
      ))}
    </div>
  );
}
