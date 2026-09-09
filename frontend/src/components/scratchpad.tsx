"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckIcon, HistoryIcon, PencilIcon, RotateCcwIcon, XIcon } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  apiFetch,
  type ScratchpadDiffLine,
  type ScratchpadVersionSummary,
  type ScratchpadView,
} from "@/lib/api";
import {
  draftFromView,
  overwriteExpectedVersion,
  recordScratchpadConflict,
  shouldRenderScratchpadDiff,
  type ScratchpadDraft,
} from "@/lib/scratchpad-state";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";

function actorLabel(version: ScratchpadVersionSummary): string {
  if (version.actor.name) return version.actor.name;
  if (version.actor.kind === "dispatcher") return "Dispatcher";
  if (version.actor.kind === "system") return "System";
  return "User";
}

function DiffLine({ line }: { line: ScratchpadDiffLine }) {
  const marker = line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " ";
  return (
    <div
      className={cn(
        "grid min-w-max grid-cols-[3rem_3rem_1.5rem_1fr] font-mono text-xs leading-6",
        line.kind === "add" && "bg-primary/10",
        line.kind === "remove" && "bg-destructive/10",
        line.kind === "header" && "bg-muted text-muted-foreground",
      )}
    >
      {line.kind === "header" ? (
        <span className="col-span-4 px-3">{line.text}</span>
      ) : (
        <>
          <span className="select-none px-2 text-right text-muted-foreground">
            {line.old_line ?? ""}
          </span>
          <span className="select-none px-2 text-right text-muted-foreground">
            {line.new_line ?? ""}
          </span>
          <span className="select-none text-center text-muted-foreground">{marker}</span>
          <span className="whitespace-pre pr-3">{line.text || " "}</span>
        </>
      )}
    </div>
  );
}

export function ScratchpadPane() {
  const [view, setView] = useState<ScratchpadView | null>(null);
  const [history, setHistory] = useState<ScratchpadVersionSummary[]>([]);
  const [tab, setTab] = useState("document");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<ScratchpadDraft | null>(null);
  const [conflict, setConflict] = useState<ScratchpadView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadHistory = useCallback(async () => {
    const response = await apiFetch("/api/scratchpad/versions");
    if (!response.ok) throw new Error();
    setHistory(await response.json());
  }, []);

  const loadVersion = useCallback(async (version?: number) => {
    const query = version === undefined ? "" : `?version=${version}`;
    const response = await apiFetch(`/api/scratchpad${query}`);
    if (!response.ok) throw new Error();
    const found: ScratchpadView = await response.json();
    setView(found);
    return found;
  }, []);

  useEffect(() => {
    let current = true;
    const initialize = async () => {
      try {
        const [scratchpadResponse, historyResponse] = await Promise.all([
          apiFetch("/api/scratchpad"),
          apiFetch("/api/scratchpad/versions"),
        ]);
        if (!scratchpadResponse.ok || !historyResponse.ok) throw new Error();
        const [found, versions] = await Promise.all([
          scratchpadResponse.json() as Promise<ScratchpadView>,
          historyResponse.json() as Promise<ScratchpadVersionSummary[]>,
        ]);
        if (current) {
          setView(found);
          setHistory(versions);
        }
      } catch {
        if (current) setError("Could not load the global scratchpad.");
      }
    };
    void initialize();
    return () => {
      current = false;
    };
  }, []);

  useEffect(() => {
    if (editing || !view || view.snapshot.version !== view.head_version) return;
    const interval = window.setInterval(() => {
      void Promise.all([loadVersion(), loadHistory()]).catch(() => undefined);
    }, 15_000);
    return () => window.clearInterval(interval);
  }, [editing, loadHistory, loadVersion, view]);

  const beginEditing = () => {
    if (!view) return;
    setDraft(draftFromView(view));
    setConflict(null);
    setError("");
    setEditing(true);
  };

  const save = async (expected: number) => {
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch("/api/scratchpad", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: draft.content, expected_version: expected }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 409) {
        const current = body.current as ScratchpadView;
        setConflict(current);
        setDraft((value) =>
          value ? recordScratchpadConflict(value, current) : value,
        );
        return;
      }
      if (!response.ok) throw new Error();
      setView(body as ScratchpadView);
      setConflict(null);
      setDraft(null);
      setEditing(false);
      await loadHistory();
    } catch {
      setError("Could not save the scratchpad.");
    } finally {
      setBusy(false);
    }
  };

  const discard = async () => {
    setBusy(true);
    setError("");
    try {
      await loadVersion();
      setConflict(null);
      setDraft(null);
      setEditing(false);
    } catch {
      setError("Could not reload the current scratchpad.");
    } finally {
      setBusy(false);
    }
  };

  const markRead = async () => {
    if (!view) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch("/api/scratchpad/read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: view.snapshot.version }),
      });
      if (!response.ok) throw new Error();
      setView(await response.json());
    } catch {
      setError("Could not mark this version as read.");
    } finally {
      setBusy(false);
    }
  };

  const openVersion = async (version: number) => {
    setBusy(true);
    setError("");
    try {
      await loadVersion(version);
      setTab("document");
    } catch {
      setError("Could not load that version.");
    } finally {
      setBusy(false);
    }
  };

  if (!view && !error) {
    return (
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold">Global scratchpad</h1>
          {view?.unread && <Badge variant="secondary">Unread changes</Badge>}
          {view && <Badge variant="outline">v{view.snapshot.version}</Badge>}
        </div>
        {view && !editing && (
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={markRead} disabled={busy}>
              <CheckIcon data-icon="inline-start" />
              Mark v{view.snapshot.version} as read
            </Button>
            <Button size="sm" onClick={beginEditing} disabled={busy}>
              <PencilIcon data-icon="inline-start" />
              Edit this version
            </Button>
          </div>
        )}
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Scratchpad error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {editing && conflict && (
        <Card>
          <CardHeader>
            <CardTitle>Scratchpad changed</CardTitle>
            <CardDescription>
              Head is now v{conflict.head_version}. Your full draft is unchanged. Overwrite
              creates a new immutable version from that draft; Discard drops it and reloads head.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            <p className="text-xs font-medium text-muted-foreground">Conflicting head</p>
            <ScrollArea className="h-32 rounded-lg border">
              <pre className="whitespace-pre-wrap p-3 text-xs">
                {conflict.snapshot.content || "No global notes."}
              </pre>
            </ScrollArea>
          </CardContent>
          <CardFooter className="gap-2">
            <Button
              onClick={() => draft && save(overwriteExpectedVersion(draft))}
              disabled={busy || !draft}
            >
              <RotateCcwIcon data-icon="inline-start" />
              Overwrite
            </Button>
            <Button variant="outline" onClick={discard} disabled={busy}>
              <XIcon data-icon="inline-start" />
              Discard
            </Button>
          </CardFooter>
        </Card>
      )}

      {editing && view && draft ? (
        <Card>
          <CardHeader>
            <CardTitle>Edit from v{draft.expectedVersion}</CardTitle>
            <CardDescription>
              This is a full-document draft. You can type freely; no lock is held.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Field>
              <FieldLabel htmlFor="scratchpad-content">Markdown</FieldLabel>
              <Textarea
                id="scratchpad-content"
                value={draft.content}
                onChange={(event) =>
                  setDraft((value) =>
                    value ? { ...value, content: event.target.value } : value,
                  )
                }
                className="min-h-96 font-mono"
                maxLength={32000}
                disabled={busy}
              />
              <FieldDescription>
                {draft.content.length.toLocaleString()} / 32,000 characters
              </FieldDescription>
            </Field>
          </CardContent>
          <CardFooter className="gap-2">
            <Button
              onClick={() => save(draft.expectedVersion)}
              disabled={busy || Boolean(conflict)}
            >
              Save
            </Button>
            <Button variant="outline" onClick={discard} disabled={busy}>
              Cancel
            </Button>
          </CardFooter>
        </Card>
      ) : view ? (
        <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
          <TabsList>
            <TabsTrigger value="document">Document</TabsTrigger>
            <TabsTrigger value="history">History</TabsTrigger>
          </TabsList>
          <TabsContent value="document">
            <Card>
              <CardHeader>
                <CardTitle>
                  {view.snapshot.version === view.head_version
                    ? "Current version"
                    : `Version ${view.snapshot.version}`}
                </CardTitle>
                <CardDescription>
                  {actorLabel(view.snapshot)} · {new Date(view.snapshot.created_at).toLocaleString()}
                  {view.snapshot.version !== view.head_version && ` · Head is v${view.head_version}`}
                </CardDescription>
              </CardHeader>
              <CardContent>
                {shouldRenderScratchpadDiff(view) ? (
                  <div className="flex flex-col gap-2">
                    <p className="text-sm text-muted-foreground">
                      Cumulative changes from globally read v{view.last_read_version} to v
                      {view.head_version}.
                    </p>
                    <ScrollArea className="h-[28rem] rounded-lg border">
                      <div className="min-w-max py-2">
                        {view.diff.map((line, index) => (
                          <DiffLine key={`${index}-${line.kind}`} line={line} />
                        ))}
                      </div>
                    </ScrollArea>
                    {view.diff_truncated && (
                      <p className="text-xs text-muted-foreground">Diff truncated at 1,000 lines.</p>
                    )}
                  </div>
                ) : view.snapshot.content ? (
                  <article className="typeset typeset-docs min-w-0">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {view.snapshot.content}
                    </ReactMarkdown>
                  </article>
                ) : (
                  <p className="text-sm text-muted-foreground">No global notes yet.</p>
                )}
              </CardContent>
            </Card>
          </TabsContent>
          <TabsContent value="history">
            <ScrollArea className="h-[32rem]">
              <div className="flex flex-col gap-2 pr-3">
                {history.map((item) => (
                  <Card key={item.version} size="sm">
                    <CardHeader>
                      <CardTitle>Version {item.version}</CardTitle>
                      <CardDescription>
                        {actorLabel(item)} · {new Date(item.created_at).toLocaleString()}
                      </CardDescription>
                      <CardAction>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => openVersion(item.version)}
                          disabled={busy}
                        >
                          <HistoryIcon data-icon="inline-start" />
                          View
                        </Button>
                      </CardAction>
                    </CardHeader>
                  </Card>
                ))}
              </div>
            </ScrollArea>
          </TabsContent>
        </Tabs>
      ) : null}
    </div>
  );
}
