"use client";

import { useEffect, useState } from "react";
import { CheckIcon, PencilIcon, PlusIcon, XIcon } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { apiFetch, type Note } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export function SpaceNotes({ spaceId }: { spaceId: string }) {
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [editing, setEditing] = useState<Note | "new" | null>(null);
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let current = true;
    apiFetch(`/api/spaces/${spaceId}/notes`)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return (await response.json()) as Note[];
      })
      .then((found) => {
        if (current) setNotes(found);
      })
      .catch(() => {
        if (current) setLoadError(true);
      });
    return () => {
      current = false;
    };
  }, [spaceId]);

  const openEditor = (note: Note | "new") => {
    setEditing(note);
    setFilename(note === "new" ? "" : note.filename);
    setContent(note === "new" ? "" : note.content);
    setError("");
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (editing === null) return;
    const nextFilename = filename.trim();
    if (
      !/^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/.test(nextFilename) ||
      nextFilename.includes("..") ||
      nextFilename.length > 100
    ) {
      setError("Use a simple .md filename, such as reviewed_issues.md.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const creating = editing === "new";
      const response = await apiFetch(
        creating
          ? `/api/spaces/${spaceId}/notes`
          : `/api/spaces/${spaceId}/notes/${encodeURIComponent(nextFilename)}`,
        {
          method: creating ? "POST" : "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            editing === "new"
              ? { filename: nextFilename, content }
              : { content, expected_revision: editing.revision },
          ),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const current = body.detail?.current as Note | undefined;
        if (response.status === 409 && current) {
          setNotes((notes) =>
            (notes ?? []).map((note) =>
              note.filename === current.filename ? current : note,
            ),
          );
          setEditing(current);
          setContent(current.content);
        }
        throw new Error(
          typeof body.detail === "string"
            ? body.detail
            : body.detail?.message ?? "Could not save note.",
        );
      }
      const saved: Note = await response.json();
      setNotes((current) =>
        creating
          ? [...(current ?? []), saved].sort((a, b) =>
              a.filename.localeCompare(b.filename),
            )
          : (current ?? []).map((note) =>
              note.filename === saved.filename ? saved : note,
            ),
      );
      setEditing(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save note.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="flex flex-col gap-3" aria-labelledby={`agent-notes-${spaceId}`}>
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 id={`agent-notes-${spaceId}`} className="text-lg font-semibold">
            Agent’s notes
          </h2>
          <p className="text-sm text-muted-foreground">
            Lean shared context for people, agents, and periodic jobs.
          </p>
        </div>
        {editing === null && (
          <Button variant="outline" size="sm" onClick={() => openEditor("new")}>
            <PlusIcon data-icon="inline-start" />
            New note
          </Button>
        )}
      </div>

      {editing !== null && (
        <form onSubmit={save}>
          <Card>
            <CardHeader>
              <CardTitle>{editing === "new" ? "New note" : filename}</CardTitle>
              <CardDescription>Markdown. Keep only durable, useful context.</CardDescription>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                <Field data-invalid={Boolean(error)}>
                  <FieldLabel htmlFor={`note-filename-${spaceId}`}>Filename</FieldLabel>
                  <Input
                    id={`note-filename-${spaceId}`}
                    value={filename}
                    onChange={(event) => setFilename(event.target.value)}
                    placeholder="reviewed_issues.md"
                    disabled={editing !== "new"}
                    maxLength={100}
                    aria-invalid={Boolean(error)}
                  />
                </Field>
                <Field data-invalid={Boolean(error)}>
                  <FieldLabel htmlFor={`note-content-${spaceId}`}>Markdown</FieldLabel>
                  <Textarea
                    id={`note-content-${spaceId}`}
                    value={content}
                    onChange={(event) => setContent(event.target.value)}
                    className="min-h-40 resize-y font-mono"
                    maxLength={32000}
                    aria-invalid={Boolean(error)}
                  />
                  <FieldError>{error}</FieldError>
                </Field>
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    disabled={saving}
                    onClick={() => setEditing(null)}
                  >
                    <XIcon data-icon="inline-start" />
                    Cancel
                  </Button>
                  <Button type="submit" disabled={saving}>
                    <CheckIcon data-icon="inline-start" />
                    {saving ? "Saving" : "Save"}
                  </Button>
                </div>
              </FieldGroup>
            </CardContent>
          </Card>
        </form>
      )}

      {notes === null && !loadError && (
        <p className="text-sm text-muted-foreground">Loading notes…</p>
      )}
      {loadError && <p className="text-sm text-destructive">Could not load notes.</p>}
      {notes?.length === 0 && editing === null && (
        <p className="text-sm text-muted-foreground">No notes yet.</p>
      )}
      {notes?.map((note) => (
        <Card key={note.filename}>
          <CardHeader>
            <CardTitle className="font-mono">{note.filename}</CardTitle>
            <CardDescription>
              Updated {new Date(note.updated_at).toLocaleString()}
            </CardDescription>
            <CardAction>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={`Edit ${note.filename}`}
                disabled={editing !== null}
                onClick={() => openEditor(note)}
              >
                <PencilIcon />
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {note.content ? (
              <article className="typeset typeset-docs min-w-0">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{ img: () => null }}
                >
                  {note.content}
                </ReactMarkdown>
              </article>
            ) : (
              <p className="text-sm text-muted-foreground">Empty note.</p>
            )}
          </CardContent>
        </Card>
      ))}
    </section>
  );
}
