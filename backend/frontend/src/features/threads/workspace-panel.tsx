import { useEffect, useRef, useState } from "react";
import {
  ChevronRight,
  File,
  FileQuestion,
  Folder,
  FolderOpen,
  Link,
  LoaderCircle,
} from "lucide-react";
import { FilePreviewSkeleton } from "@/features/repository/repository-skeletons";
import { ResizeHandle } from "@/components/resize-handle";
import { useApi } from "@/hooks/use-api";
import type {
  SandboxDirectory,
  SandboxEntry,
  SandboxFile,
} from "@/lib/api-types";

const refreshInterval = 2_000;

function endpoint(chatId: string, path: string, file = false) {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  return `/api/chats/${encodeURIComponent(chatId)}/thread/filesystem${file ? "/file" : ""}?${params}`;
}

type TreeProps = {
  chatId: string;
  visible: boolean;
  waitForCreation: boolean;
  expanded: Set<string>;
  selected: string;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
};

function EntryIcon({ entry, open }: { entry: SandboxEntry; open?: boolean }) {
  const className = "size-3.5 shrink-0 text-muted-foreground";
  if (entry.kind === "directory")
    return open ? (
      <FolderOpen aria-hidden className={className} />
    ) : (
      <Folder aria-hidden className={className} />
    );
  if (entry.kind === "file") return <File aria-hidden className={className} />;
  if (entry.kind === "symlink")
    return <Link aria-hidden className={className} />;
  return <FileQuestion aria-hidden className={className} />;
}

function DirectoryBranch({
  path,
  name,
  root = false,
  ...props
}: TreeProps & {
  path: string;
  name: string;
  root?: boolean;
}) {
  const open = root || props.expanded.has(path);
  const resource = useApi<SandboxDirectory>(
    open ? endpoint(props.chatId, path) : null,
    props.visible ? refreshInterval : 0,
    {
      keepPreviousData: true,
      waitForCreation: props.waitForCreation,
    },
  );
  const { mutate: refreshDirectory } = resource;
  useEffect(() => {
    if (open && props.visible) void refreshDirectory();
  }, [open, props.visible, refreshDirectory]);

  const contents = open ? (
    <ul className={root ? "space-y-0.5" : "ml-3 space-y-0.5 border-l pl-1"}>
      {resource.data?.entries.map((entry) =>
        entry.kind === "directory" ? (
          <DirectoryBranch
            key={entry.path}
            {...props}
            path={entry.path}
            name={entry.name}
          />
        ) : (
          <li key={entry.path}>
            {entry.kind === "file" ? (
              <button
                type="button"
                className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted ${props.selected === entry.path ? "bg-muted font-medium" : ""}`}
                aria-current={
                  props.selected === entry.path ? "true" : undefined
                }
                title={entry.path}
                onClick={() => props.onSelect(entry.path)}
              >
                <EntryIcon entry={entry} />
                <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              </button>
            ) : (
              <span
                className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground"
                title={`${entry.path} (${entry.kind}; preview unavailable)`}
              >
                <EntryIcon entry={entry} />
                <span className="min-w-0 flex-1 truncate">{entry.name}</span>
                <span className="text-[10px]">{entry.kind}</span>
              </span>
            )}
          </li>
        ),
      )}
      {resource.error ? (
        <li className="px-2 py-1.5 text-xs text-destructive" role="alert">
          {resource.error.message}
        </li>
      ) : null}
      {!resource.data && !resource.error ? (
        <li
          className="flex items-center gap-1.5 px-2 py-1.5 text-xs text-muted-foreground"
          role="status"
        >
          <LoaderCircle
            className="size-3 motion-safe:animate-spin"
            aria-hidden
          />
          {props.waitForCreation ? "Starting sandbox…" : "Loading…"}
        </li>
      ) : null}
      {resource.data?.truncated ? (
        <li className="px-2 py-1.5 text-xs text-muted-foreground">
          Directory truncated after 2,000 entries.
        </li>
      ) : null}
    </ul>
  ) : null;

  if (root) return contents;
  const entry: SandboxEntry = { path, name, kind: "directory" };
  return (
    <li>
      <button
        type="button"
        className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs hover:bg-muted"
        aria-expanded={open}
        title={path}
        onClick={() => props.onToggle(path)}
      >
        <ChevronRight
          aria-hidden
          className={`size-3 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
        />
        <EntryIcon entry={entry} open={open} />
        <span className="min-w-0 flex-1 truncate">{name}</span>
      </button>
      {contents}
    </li>
  );
}

export default function WorkspacePanel({
  chatId,
  visible,
  waitForCreation = false,
}: {
  chatId: string;
  visible: boolean;
  waitForCreation?: boolean;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [selected, setSelected] = useState("");
  const panelRef = useRef<HTMLElement>(null);
  const treeRef = useRef<HTMLDivElement>(null);
  const file = useApi<SandboxFile>(
    selected ? endpoint(chatId, selected, true) : null,
    visible ? refreshInterval : 0,
  );
  const { mutate: refreshFile } = file;
  useEffect(() => {
    if (selected && visible) void refreshFile();
  }, [selected, visible, refreshFile]);

  function toggle(path: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  return (
    <section
      ref={panelRef}
      className="grid min-h-0 flex-1 grid-rows-[var(--workspace-tree-height)_1px_minmax(0,1fr)] [--workspace-tree-height:42%]"
    >
      <div
        ref={treeRef}
        className="min-h-0 overflow-auto p-2"
        aria-label="Sandbox workspace files"
      >
        <div className="px-2 py-1.5 font-mono text-xs text-muted-foreground">
          /workspace
        </div>
        <DirectoryBranch
          root
          path=""
          name="workspace"
          chatId={chatId}
          visible={visible}
          waitForCreation={waitForCreation}
          expanded={expanded}
          selected={selected}
          onToggle={toggle}
          onSelect={setSelected}
        />
      </div>
      <ResizeHandle
        containerRef={panelRef}
        paneRef={treeRef}
        variable="--workspace-tree-height"
        direction={1}
        orientation="horizontal"
        label="Resize workspace tree"
        minimum={72}
        maximum={640}
        minimumContent={96}
        defaultValue={240}
      />
      <div className="flex min-h-0 min-w-0 flex-col">
        <div
          className="shrink-0 truncate border-b px-4 py-2.5 font-mono text-xs"
          title={selected}
        >
          {selected ? `/workspace/${selected}` : "Select a file"}
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {!selected ? (
            <p className="p-5 text-sm text-muted-foreground">
              Choose a file to inspect its live sandbox contents.
            </p>
          ) : !file.data && !file.error ? (
            <FilePreviewSkeleton label="Loading sandbox file" />
          ) : file.error ? (
            <p className="p-5 text-sm text-destructive" role="alert">
              {file.error.message}
            </p>
          ) : file.data?.text === null ? (
            <p className="p-5 text-sm text-muted-foreground">
              {file.data.notice}
            </p>
          ) : (
            <>
              <pre
                className="min-w-max p-5 font-mono text-xs leading-6"
                aria-label="Sandbox file contents"
              >
                {file.data?.text || "(empty file)"}
              </pre>
              {file.data?.truncated ? (
                <p className="px-5 pb-5 text-xs text-muted-foreground">
                  {file.data.notice}
                </p>
              ) : null}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
