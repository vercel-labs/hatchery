"use client";

import { useEffect, useState } from "react";
import {
  CheckIcon,
  PencilIcon,
  PlusIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { apiFetch, type Note, type NoteSummary } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

type EditingNote = "new" | { filename: string; revision: number };
type NoteItem = Note | NoteSummary;

const MAX_NOTE_CONTENT_BYTES = 1_000_000;

export function SpaceNotes({ spaceId }: { spaceId: string }) {
  const [notes, setNotes] = useState<NoteItem[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [editing, setEditing] = useState<EditingNote | null>(null);
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [conflict, setConflict] = useState<Note | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [noteError, setNoteError] = useState<{ filename: string; message: string } | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let current = true;
    apiFetch(`/api/spaces/${spaceId}/notes`)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return (await response.json()) as NoteSummary[];
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
    setEditing(
      note === "new"
        ? "new"
        : { filename: note.filename, revision: note.revision },
    );
    setFilename(note === "new" ? "" : note.filename);
    setContent(note === "new" ? "" : note.content);
    setConflict(null);
    setConfirmingDelete(null);
    setError("");
  };

  const closeEditor = () => {
    setEditing(null);
    setConflict(null);
    setError("");
  };

  const toggleNote = async (note: NoteItem) => {
    if (selected === note.filename) {
      setSelected(null);
      return;
    }
    setSelected(note.filename);
    setConfirmingDelete(null);
    setError("");
    setNoteError(null);
    if ("content" in note) return;
    try {
      const response = await apiFetch(
        `/api/spaces/${spaceId}/notes/${encodeURIComponent(note.filename)}`,
      );
      if (!response.ok) throw new Error("Could not load note.");
      const loaded = (await response.json()) as Note;
      setNotes((found) =>
        (found ?? []).map((item) =>
          item.filename === loaded.filename ? loaded : item,
        ),
      );
    } catch (caught) {
      setNoteError({
        filename: note.filename,
        message: caught instanceof Error ? caught.message : "Could not load note.",
      });
    }
  };

  const persist = async (override: boolean) => {
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
    const contentBytes = new TextEncoder().encode(JSON.stringify(content)).length - 2;
    if (contentBytes > MAX_NOTE_CONTENT_BYTES) {
      setError(
        `Markdown must be at most ${MAX_NOTE_CONTENT_BYTES.toLocaleString()} JSON-encoded UTF-8 bytes; got ${contentBytes.toLocaleString()}.`,
      );
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
            creating
              ? { filename: nextFilename, content }
              : {
                  content,
                  expected_revision:
                    override && conflict ? conflict.revision : editing.revision,
                  override,
                },
          ),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const current = body.detail?.current as Note | undefined;
        if (response.status === 409 && current) {
          setNotes((found) =>
            (found ?? []).map((note) =>
              note.filename === current.filename ? current : note,
            ),
          );
          setConflict(current);
        }
        throw new Error(
          typeof body.detail === "string"
            ? body.detail
            : body.detail?.message ?? "Could not save note.",
        );
      }
      const saved: Note = await response.json();
      setNotes((found) =>
        creating
          ? [...(found ?? []), saved].sort((a, b) =>
              a.filename.localeCompare(b.filename),
            )
          : (found ?? []).map((note) =>
              note.filename === saved.filename ? saved : note,
            ),
      );
      setSelected(saved.filename);
      closeEditor();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save note.");
    } finally {
      setSaving(false);
    }
  };

  const save = (event: React.FormEvent) => {
    event.preventDefault();
    void persist(false);
  };

  const deleteNote = async (note: NoteSummary) => {
    setDeleting(true);
    setError("");
    try {
      const response = await apiFetch(
        `/api/spaces/${spaceId}/notes/${encodeURIComponent(note.filename)}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error("Could not delete note.");
      setNotes((found) =>
        (found ?? []).filter((item) => item.filename !== note.filename),
      );
      setSelected(null);
      setConfirmingDelete(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not delete note.");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <section
      className="mt-6 flex flex-col gap-3"
      aria-labelledby={`agent-notes-${spaceId}`}
    >
      <div className="flex h-7 items-center justify-between px-1">
        <h2
          id={`agent-notes-${spaceId}`}
          className="text-xs font-medium text-muted-foreground"
        >
          Agent notes
        </h2>
        {editing === null && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Create note"
            title="Create note"
            onClick={() => openEditor("new")}
          >
            <PlusIcon />
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
                    aria-invalid={Boolean(error)}
                  />
                  <FieldDescription>
                    Up to 1,000,000 JSON-encoded UTF-8 bytes.
                  </FieldDescription>
                  <FieldError>{error}</FieldError>
                </Field>
                {conflict && (
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    <p className="mr-auto text-sm text-muted-foreground">
                      Latest revision: {conflict.revision}. Overwrite only after reviewing
                      the latest note.
                    </p>
                    <Button
                      type="button"
                      variant="destructive"
                      disabled={saving}
                      onClick={() => void persist(true)}
                    >
                      Overwrite current version
                    </Button>
                  </div>
                )}
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    disabled={saving}
                    onClick={closeEditor}
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
      {notes && notes.length > 0 && (
        <ul className="ml-2 border-l border-border/60">
          {notes.map((note) => {
            const open = selected === note.filename;
            return (
              <li
                key={note.filename}
                className="relative pl-4 before:absolute before:top-3 before:left-0 before:w-3 before:border-t before:border-border/60"
              >
                <div className="flex min-h-6 items-center gap-1 text-muted-foreground">
                  <Button
                    variant="ghost"
                    aria-expanded={open}
                    className="h-auto justify-start px-0"
                    disabled={editing !== null}
                    onClick={() => void toggleNote(note)}
                  >
                    <span className="font-mono">{note.filename}</span>
                  </Button>
                  {open && editing === null && "content" in note && (
                    <>
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label={`Edit ${note.filename}`}
                        title={`Edit ${note.filename}`}
                        onClick={() => openEditor(note)}
                      >
                        <PencilIcon />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label={`Delete ${note.filename}`}
                        title={`Delete ${note.filename}`}
                        onClick={() => setConfirmingDelete(note.filename)}
                      >
                        <Trash2Icon />
                      </Button>
                    </>
                  )}
                </div>
                {open && (
                  <div className="flex flex-col gap-2 pb-3 pl-[1.875rem] text-muted-foreground">
                    {"content" in note ? (
                      note.content ? (
                        <article className="typeset typeset-docs min-w-0 [--color-foreground:var(--muted-foreground)]">
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm]}
                            components={{ img: () => null }}
                          >
                            {note.content}
                          </ReactMarkdown>
                        </article>
                      ) : (
                        <p className="text-sm">Empty note.</p>
                      )
                    ) : noteError?.filename === note.filename ? (
                      <p className="text-sm text-destructive">{noteError.message}</p>
                    ) : (
                      <p className="text-sm">Loading note…</p>
                    )}
                    {confirmingDelete === note.filename && (
                      <div className="flex flex-wrap items-center gap-2 text-sm" role="group" aria-label={`Confirm deletion of ${note.filename}`}>
                        <span>Delete this note?</span>
                        <Button
                          variant="ghost"
                          size="xs"
                          disabled={deleting}
                          onClick={() => setConfirmingDelete(null)}
                        >
                          Cancel
                        </Button>
                        <Button
                          variant="destructive"
                          size="xs"
                          disabled={deleting}
                          onClick={() => void deleteNote(note)}
                        >
                          {deleting ? "Deleting" : "Delete"}
                        </Button>
                      </div>
                    )}
                    {error && editing === null && (
                      <p className="text-sm text-destructive">{error}</p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
