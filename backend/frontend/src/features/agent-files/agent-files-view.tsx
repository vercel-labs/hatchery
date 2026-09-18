import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckIcon, FilePlusIcon, RefreshCwIcon, Trash2Icon } from "lucide-react";

import { apiFetch } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { StorageFileTree } from "./storage-file-tree";


type Snapshot = {
  agent_slug: string;
  revision: string | null;
  files: string[];
};

type AgentFile = {
  agent_slug: string;
  path: string;
  content: string;
  revision: string;
};

function operationId() {
  return `ui:${crypto.randomUUID()}`;
}

export function AgentFilesView({ agentId }: { agentId: string }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selected, setSelected] = useState("AGENTS.md");
  const [content, setContent] = useState("");
  const [savedContent, setSavedContent] = useState("");
  const [filter, setFilter] = useState("");
  const [newPath, setNewPath] = useState("");
  const [loadingFile, setLoadingFile] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadSnapshot = useCallback(async () => {
    const response = await apiFetch(`/api/agents/${agentId}/files`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail ?? "Could not load agent files.");
    }
    const next = (await response.json()) as Snapshot;
    setSnapshot(next);
    setSelected((current) =>
      next.files.includes(current)
        ? current
        : next.files.includes("AGENTS.md")
          ? "AGENTS.md"
          : (next.files[0] ?? ""),
    );
  }, [agentId]);

  useEffect(() => {
    let current = true;
    apiFetch(`/api/agents/${agentId}/files`)
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail ?? "Could not load agent files.");
        }
        return (await response.json()) as Snapshot;
      })
      .then((next) => {
        if (!current) return;
        setSnapshot(next);
        setSelected(
          next.files.includes("AGENTS.md") ? "AGENTS.md" : (next.files[0] ?? ""),
        );
      })
      .catch((reason) => {
        if (current) {
          setError(
            reason instanceof Error ? reason.message : "Could not load agent files.",
          );
        }
      });
    return () => {
      current = false;
    };
  }, [agentId]);

  useEffect(() => {
    if (!selected || !snapshot?.revision) return;
    let current = true;
    apiFetch(
      `/api/agents/${agentId}/file?path=${encodeURIComponent(selected)}&revision=${snapshot.revision}`,
    )
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not load this file.");
        return (await response.json()) as AgentFile;
      })
      .then((file) => {
        if (!current) return;
        setContent(file.content);
        setSavedContent(file.content);
      })
      .catch((reason) => {
        if (current) setError(reason instanceof Error ? reason.message : "Could not load this file.");
      })
      .finally(() => {
        if (current) setLoadingFile(false);
      });
    return () => {
      current = false;
    };
  }, [agentId, selected, snapshot?.revision]);

  const files = useMemo(
    () =>
      (snapshot?.files ?? [])
        .filter((path) => path.toLowerCase().includes(filter.trim().toLowerCase()))
        .map((path) => ({ path })),
    [filter, snapshot?.files],
  );

  const save = async () => {
    if (!selected || !snapshot) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(`/api/agents/${agentId}/file`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: selected,
          content,
          operation_id: operationId(),
          expected_revision: snapshot.revision,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail?.message ?? body.detail ?? "Could not save file.");
      setSnapshot(body as Snapshot);
      setSavedContent(content);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save file.");
    } finally {
      setBusy(false);
    }
  };

  const createFile = async (event: React.FormEvent) => {
    event.preventDefault();
    const path = newPath.trim();
    if (!path || !snapshot) return;
    setSelected(path);
    setContent("");
    setSavedContent("");
    setNewPath("");
  };

  const deleteFile = async () => {
    if (!selected || selected === "AGENTS.md" || !snapshot) return;
    if (!window.confirm(`Delete ${selected}?`)) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(`/api/agents/${agentId}/file`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: selected,
          operation_id: operationId(),
          expected_revision: snapshot.revision,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail?.message ?? body.detail ?? "Could not delete file.");
      const next = body as Snapshot;
      setSnapshot(next);
      setSelected(next.files.includes("AGENTS.md") ? "AGENTS.md" : (next.files[0] ?? ""));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not delete file.");
    } finally {
      setBusy(false);
    }
  };

  if (!snapshot && !error) {
    return (
      <div className="grid min-h-96 w-full grid-cols-1 gap-4 md:grid-cols-[16rem_minmax(0,1fr)]">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
    );
  }

  if (!snapshot) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Agent storage unavailable</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }

  return (
    <section className="flex min-h-0 w-full flex-1 flex-col overflow-hidden rounded-xl border">
      <header className="flex flex-wrap items-center gap-2 border-b p-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-medium">Agent files</h2>
          <p className="truncate text-xs text-muted-foreground" title={snapshot.revision ?? undefined}>
            {snapshot.agent_slug} · {snapshot.revision?.slice(0, 12) ?? "empty repository"}
          </p>
        </div>
        <Button variant="ghost" size="icon-sm" onClick={() => void loadSnapshot()} aria-label="Refresh files">
          <RefreshCwIcon />
        </Button>
      </header>
      {error ? (
        <Alert variant="destructive" className="m-3">
          <AlertTitle>File operation failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      <div className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(12rem,0.45fr)_minmax(20rem,1fr)] md:grid-cols-[16rem_minmax(0,1fr)] md:grid-rows-1">
        <aside className="flex min-h-0 flex-col border-b md:border-r md:border-b-0">
          <div className="flex flex-col gap-2 border-b p-3">
            <Input aria-label="Filter agent files" placeholder="Find a file…" value={filter} onChange={(event) => setFilter(event.target.value)} />
            <form className="flex gap-2" onSubmit={createFile}>
              <Input aria-label="New file path" placeholder="memories/topic.md" value={newPath} onChange={(event) => setNewPath(event.target.value)} />
              <Button type="submit" size="icon" variant="outline" aria-label="Create file" disabled={!newPath.trim()}>
                <FilePlusIcon />
              </Button>
            </form>
          </div>
          <StorageFileTree
            className="flex-1"
            files={files}
            selectedPath={selected}
            onSelect={(file) => {
              setLoadingFile(true);
              setSelected(file.path);
            }}
          />
        </aside>
        <div className="flex min-h-0 flex-col">
          <div className="flex items-center gap-2 border-b px-4 py-2">
            <code className="min-w-0 flex-1 truncate text-xs">{selected || "Select a file"}</code>
            {selected && selected !== "AGENTS.md" ? (
              <Button variant="ghost" size="icon-sm" onClick={() => void deleteFile()} disabled={busy} aria-label={`Delete ${selected}`}>
                <Trash2Icon />
              </Button>
            ) : null}
            <Button onClick={() => void save()} disabled={!selected || busy || content === savedContent} size="sm">
              <CheckIcon data-icon="inline-start" />
              {busy ? "Saving" : "Save"}
            </Button>
          </div>
          <FieldGroup className="min-h-0 flex-1 p-4">
            <Field data-invalid={Boolean(error)} className="min-h-0 flex-1">
              <FieldLabel htmlFor={`agent-file-${agentId}`}>Contents</FieldLabel>
              {loadingFile ? (
                <Skeleton className="min-h-80 flex-1" />
              ) : (
                <Textarea
                  id={`agent-file-${agentId}`}
                  value={content}
                  onChange={(event) => setContent(event.target.value)}
                  className="min-h-80 flex-1 resize-none font-mono"
                  aria-invalid={Boolean(error)}
                  disabled={!selected}
                />
              )}
              <FieldError>{error}</FieldError>
            </Field>
          </FieldGroup>
        </div>
      </div>
    </section>
  );
}
