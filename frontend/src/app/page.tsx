"use client";

import { useCallback, useEffect, useState } from "react";
import {
  BookMarkedIcon,
  CheckIcon,
  FolderGitIcon,
  LinkIcon,
  PencilIcon,
  PlusIcon,
  TerminalIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Chat, Resource, Space } from "@/lib/api";
import type { ChatUIMessage } from "@/lib/messages";
import { ChatView } from "@/components/chat";
import { TerminalPane, type CoderTask } from "@/components/terminal-pane";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarProvider,
  SidebarSeparator,
  SidebarTrigger,
} from "@/components/ui/sidebar";

type Selection =
  | { kind: "space"; id: string }
  | { kind: "chat"; id: string }
  | null;

function Dot({ color }: { color: string | undefined }) {
  return (
    <span
      className="size-2 shrink-0 rounded-full"
      style={{ backgroundColor: color ?? "var(--muted-foreground)" }}
    />
  );
}

export default function Home() {
  const [spaces, setSpaces] = useState<Space[] | null>(null);
  const [chats, setChats] = useState<Chat[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [selection, setSelection] = useState<Selection>(null);
  const [addingSpace, setAddingSpace] = useState(false);
  const [spaceName, setSpaceName] = useState("");
  // set by space clicks only; chat clicks leave the order alone
  const [sortSpaceId, setSortSpaceId] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      const [s, c] = await Promise.all([
        fetch("/api/spaces"),
        fetch("/api/chats"),
      ]);
      if (!s.ok || !c.ok) throw new Error("backend unreachable");
      setSpaces(await s.json());
      setChats(await c.json());
    };
    load().catch(() => setFailed(true));
  }, []);

  const colorOf = (spaceId: string) =>
    spaces?.find((s) => s.id === spaceId)?.color;

  const selectedSpace =
    selection?.kind === "space"
      ? (spaces?.find((s) => s.id === selection.id) ?? null)
      : null;
  const selectedChat =
    selection?.kind === "chat"
      ? (chats?.find((c) => c.id === selection.id) ?? null)
      : null;

  const sortedChats =
    chats && sortSpaceId
      ? [...chats].sort(
          (a, b) =>
            Number(b.space_id === sortSpaceId) -
            Number(a.space_id === sortSpaceId),
        )
      : chats;

  const createSpace = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!spaceName.trim()) return;
    const res = await fetch("/api/spaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: spaceName }),
    });
    if (!res.ok) return;
    const space: Space = await res.json();
    setSpaces((current) => [...(current ?? []), space]);
    setSelection({ kind: "space", id: space.id });
    setSortSpaceId(space.id);
    setSpaceName("");
    setAddingSpace(false);
  };

  const deleteSpace = async (space: Space) => {
    if (!window.confirm(`Remove ${space.name}?`)) return;
    const res = await fetch(`/api/spaces/${space.id}`, { method: "DELETE" });
    if (res.status === 409) {
      window.alert("Remove this space's chats first.");
      return;
    }
    if (!res.ok) return;
    setSpaces((current) => current?.filter((item) => item.id !== space.id) ?? null);
    if (selection?.kind === "space" && selection.id === space.id) setSelection(null);
    if (sortSpaceId === space.id) setSortSpaceId(null);
  };

  const createChat = async () => {
    const spaceId = selectedSpace?.id ?? sortSpaceId ?? spaces?.[0]?.id ?? null;
    const res = await fetch("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ space_id: spaceId }),
    });
    if (!res.ok) return;
    const chat: Chat = await res.json();
    setChats((prev) => [chat, ...(prev ?? [])]);
    setSelection({ kind: "chat", id: chat.id });
  };

  return (
    <SidebarProvider className="h-svh overflow-hidden">
      <Sidebar>
        <SidebarHeader className="px-4 py-3">
          <span className="text-sm font-semibold">hatchery</span>
          <span className="text-xs text-muted-foreground">
            a software factory
          </span>
        </SidebarHeader>
        <SidebarSeparator />

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Spaces</SidebarGroupLabel>
            <SidebarGroupAction
              title="New space"
              aria-label="New space"
              onClick={() => setAddingSpace(true)}
            >
              <PlusIcon />
            </SidebarGroupAction>
            <SidebarGroupContent>
              {addingSpace && (
                <form className="flex gap-1 px-2 pb-1" onSubmit={createSpace}>
                  <Input
                    autoFocus
                    value={spaceName}
                    onChange={(event) => setSpaceName(event.target.value)}
                    placeholder="Space name"
                    aria-label="Space name"
                    className="h-7"
                  />
                  <Button type="submit" size="icon-xs" disabled={!spaceName.trim()}>
                    <CheckIcon />
                    <span className="sr-only">Add space</span>
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => {
                      setAddingSpace(false);
                      setSpaceName("");
                    }}
                  >
                    <XIcon />
                    <span className="sr-only">Cancel</span>
                  </Button>
                </form>
              )}
              <SidebarMenu>
                {spaces === null
                  ? Array.from({ length: failed ? 0 : 2 }).map((_, i) => (
                      <SidebarMenuItem key={i}>
                        <SidebarMenuSkeleton />
                      </SidebarMenuItem>
                    ))
                  : spaces.map((space) => (
                      <SidebarMenuItem key={space.id}>
                        <SidebarMenuButton
                          isActive={selectedSpace?.id === space.id}
                          onClick={() => {
                            setSelection({ kind: "space", id: space.id });
                            setSortSpaceId(space.id);
                          }}
                          tooltip={space.name}
                        >
                          <Dot color={space.color} />
                          <span className="truncate">{space.name}</span>
                        </SidebarMenuButton>
                        <SidebarMenuAction
                          showOnHover
                          aria-label={`Remove ${space.name}`}
                          title={`Remove ${space.name}`}
                          onClick={() => deleteSpace(space)}
                        >
                          <Trash2Icon />
                        </SidebarMenuAction>
                      </SidebarMenuItem>
                    ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>

          <SidebarGroup>
            <SidebarGroupLabel>Chats</SidebarGroupLabel>
            <SidebarGroupAction title="New chat" onClick={createChat}>
              <PlusIcon />
            </SidebarGroupAction>
            <SidebarGroupContent>
              <SidebarMenu>
                {sortedChats === null
                  ? Array.from({ length: failed ? 0 : 4 }).map((_, i) => (
                      <SidebarMenuItem key={i}>
                        <SidebarMenuSkeleton />
                      </SidebarMenuItem>
                    ))
                  : sortedChats.map((chat) => (
                      <SidebarMenuItem key={chat.id}>
                        <SidebarMenuButton
                          isActive={selectedChat?.id === chat.id}
                          onClick={() =>
                            setSelection({ kind: "chat", id: chat.id })
                          }
                          tooltip={chat.title}
                        >
                          <Dot color={colorOf(chat.space_id)} />
                          <span className="truncate">{chat.title}</span>
                        </SidebarMenuButton>
                      </SidebarMenuItem>
                    ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>

      <SidebarInset>
        <header className="flex h-14 items-center gap-2 border-b px-4">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-4" />
          {selection && (
            <Dot
              color={
                selectedSpace?.color ??
                (selectedChat ? colorOf(selectedChat.space_id) : undefined)
              }
            />
          )}
          <span className="text-sm font-medium">
            {selectedSpace?.name ?? selectedChat?.title ?? "hatchery"}
          </span>
        </header>
        {selectedChat && !failed ? (
          <LiveChat key={selectedChat.id} chat={selectedChat} />
        ) : (
          <div className="flex-1 overflow-y-auto p-6 md:p-10">
            {failed ? (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Backend unreachable</EmptyTitle>
                  <EmptyDescription>
                    Could not load spaces and chats. Locally: run `uv run
                    dev.py` in backend/ and reload.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : selectedSpace ? (
              <SpacePane
                space={selectedSpace}
                onChange={(updated) =>
                  setSpaces((current) =>
                    current?.map((space) =>
                      space.id === updated.id ? updated : space,
                    ) ?? null,
                  )
                }
              />
            ) : (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Nothing selected</EmptyTitle>
                  <EmptyDescription>
                    Pick a space or a chat from the sidebar.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
          </div>
        )}
      </SidebarInset>
    </SidebarProvider>
  );
}

const resourceIcon = {
  repo: FolderGitIcon,
  reference: BookMarkedIcon,
  link: LinkIcon,
} as const;

function ResourceCard({ resource }: { resource: Resource }) {
  const Icon =
    resourceIcon[resource.kind as keyof typeof resourceIcon] ?? LinkIcon;
  return (
    <a href={resource.url} target="_blank" rel="noreferrer">
      <Card className="flex-row items-center gap-3 p-3 transition-colors hover:bg-accent/50">
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">
            {resource.title}
          </span>
          <span className="truncate text-xs text-muted-foreground">
            {new URL(resource.url).hostname}
          </span>
        </div>
      </Card>
    </a>
  );
}

function SpacePane({
  space,
  onChange,
}: {
  space: Space;
  onChange: (space: Space) => void;
}) {
  const [editingDocument, setEditingDocument] = useState(false);
  const [documentName, setDocumentName] = useState(space.name);
  const [documentAbout, setDocumentAbout] = useState(space.about);
  const [savingDocument, setSavingDocument] = useState(false);
  const [documentError, setDocumentError] = useState("");
  const [editingResources, setEditingResources] = useState(false);
  const [repos, setRepos] = useState(space.repos);
  const [links, setLinks] = useState(space.resources);
  const [kind, setKind] = useState<"repo" | "link">("repo");
  const [resourceTitle, setResourceTitle] = useState("");
  const [url, setUrl] = useState("");
  const [savingResources, setSavingResources] = useState(false);
  const [resourceError, setResourceError] = useState("");

  const resources = [
    ...space.repos.map((repo) => ({
      title: repo,
      url: `https://github.com/${repo}`,
      kind: "repo",
    })),
    ...space.resources,
  ];

  const startEditingDocument = () => {
    setDocumentName(space.name);
    setDocumentAbout(space.about);
    setDocumentError("");
    setEditingDocument(true);
  };

  const saveDocument = async () => {
    if (!documentName.trim()) {
      setDocumentError("Title is required.");
      return;
    }
    setSavingDocument(true);
    setDocumentError("");
    try {
      const response = await fetch(`/api/spaces/${space.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: documentName, about: documentAbout }),
      });
      if (!response.ok) throw new Error();
      onChange(await response.json());
      setEditingDocument(false);
    } catch {
      setDocumentError("Could not save space.");
    } finally {
      setSavingDocument(false);
    }
  };

  const startEditingResources = () => {
    setRepos(space.repos);
    setLinks(space.resources);
    setResourceError("");
    setEditingResources(true);
  };

  const addResource = (event: React.FormEvent) => {
    event.preventDefault();
    setResourceError("");
    if (kind === "repo") {
      const repo = url.trim();
      if (!/^[^/\s]+\/[^/\s]+$/.test(repo)) {
        setResourceError("Use owner/repo form.");
        return;
      }
      if (!repos.includes(repo)) setRepos([...repos, repo]);
    } else {
      const nextTitle = resourceTitle.trim();
      const nextUrl = url.trim();
      try {
        const parsed = new URL(nextUrl);
        if (!nextTitle || !["http:", "https:"].includes(parsed.protocol)) {
          throw new Error();
        }
      } catch {
        setResourceError("Add a title and a valid http(s) URL.");
        return;
      }
      if (!links.some((resource) => resource.url === nextUrl)) {
        setLinks([...links, { title: nextTitle, url: nextUrl, kind: "link" }]);
      }
    }
    setResourceTitle("");
    setUrl("");
  };

  const saveResources = async () => {
    setSavingResources(true);
    setResourceError("");
    try {
      const response = await fetch(`/api/spaces/${space.id}/resources`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repos, resources: links }),
      });
      if (!response.ok) throw new Error();
      onChange(await response.json());
      setEditingResources(false);
    } catch {
      setResourceError("Could not save resources.");
    } finally {
      setSavingResources(false);
    }
  };

  return (
    <div className="mx-auto grid w-full max-w-6xl gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <section className="mx-auto flex w-full max-w-2xl min-w-0 flex-col gap-6">
        {editingDocument ? (
          <FieldGroup>
            <Field data-invalid={Boolean(documentError)}>
              <FieldLabel htmlFor={`space-name-${space.id}`}>Title</FieldLabel>
              <Input
                id={`space-name-${space.id}`}
                value={documentName}
                onChange={(event) => setDocumentName(event.target.value)}
                aria-invalid={Boolean(documentError)}
              />
            </Field>
            <Field data-invalid={Boolean(documentError)}>
              <FieldLabel htmlFor={`space-about-${space.id}`}>Markdown</FieldLabel>
              <Textarea
                id={`space-about-${space.id}`}
                value={documentAbout}
                onChange={(event) => setDocumentAbout(event.target.value)}
                className="min-h-96 resize-y font-mono"
                aria-invalid={Boolean(documentError)}
              />
              <FieldError>{documentError}</FieldError>
            </Field>
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                disabled={savingDocument}
                onClick={() => setEditingDocument(false)}
              >
                <XIcon />
                Cancel
              </Button>
              <Button disabled={savingDocument} onClick={saveDocument}>
                <CheckIcon />
                {savingDocument ? "Saving" : "Save"}
              </Button>
            </div>
          </FieldGroup>
        ) : (
          <>
            <div className="flex items-center justify-between gap-4">
              <h1 className="text-3xl font-semibold tracking-tight">{space.name}</h1>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Edit space"
                onClick={startEditingDocument}
              >
                <PencilIcon />
              </Button>
            </div>
            <article className="typeset typeset-docs min-w-0">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{space.about}</ReactMarkdown>
            </article>
          </>
        )}
      </section>
      <aside className="mx-auto flex w-full max-w-2xl flex-col gap-2 lg:mx-0 lg:max-w-none">
        <div className="flex h-7 items-center justify-between px-1">
          <span className="text-xs font-medium text-muted-foreground">
            Resources
          </span>
          {!editingResources && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Edit resources"
              onClick={startEditingResources}
            >
              <PencilIcon />
            </Button>
          )}
        </div>
        {editingResources ? (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              {repos.map((repo) => (
                <EditableResource
                  key={`repo:${repo}`}
                  resource={{
                    title: repo,
                    url: `https://github.com/${repo}`,
                    kind: "repo",
                  }}
                  onDelete={() => setRepos(repos.filter((item) => item !== repo))}
                />
              ))}
              {links.map((resource, index) => (
                <EditableResource
                  key={`${resource.url}:${index}`}
                  resource={resource}
                  onDelete={() => setLinks(links.filter((_, item) => item !== index))}
                />
              ))}
            </div>
            <form onSubmit={addResource}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor={`resource-kind-${space.id}`}>Add resource</FieldLabel>
                  <select
                    id={`resource-kind-${space.id}`}
                    value={kind}
                    onChange={(event) => {
                      setKind(event.target.value as "repo" | "link");
                      setResourceTitle("");
                      setUrl("");
                      setResourceError("");
                    }}
                    className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                  >
                    <option value="repo">GitHub repository</option>
                    <option value="link">Link</option>
                  </select>
                </Field>
                {kind === "link" && (
                  <Field>
                    <FieldLabel htmlFor={`resource-title-${space.id}`}>Title</FieldLabel>
                    <Input
                      id={`resource-title-${space.id}`}
                      value={resourceTitle}
                      onChange={(event) => setResourceTitle(event.target.value)}
                      placeholder="Documentation"
                    />
                  </Field>
                )}
                <Field data-invalid={Boolean(resourceError)}>
                  <FieldLabel htmlFor={`resource-url-${space.id}`}>
                    {kind === "repo" ? "Repository" : "URL"}
                  </FieldLabel>
                  <Input
                    id={`resource-url-${space.id}`}
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    placeholder={kind === "repo" ? "owner/repo" : "https://example.com"}
                    aria-invalid={Boolean(resourceError)}
                  />
                  {kind === "repo" && (
                    <FieldDescription>Enter a GitHub repository as owner/repo.</FieldDescription>
                  )}
                  <FieldError>{resourceError}</FieldError>
                </Field>
                <Button type="submit" variant="outline">
                  <PlusIcon />
                  Add
                </Button>
              </FieldGroup>
            </form>
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                disabled={savingResources}
                onClick={() => setEditingResources(false)}
              >
                <XIcon />
                Cancel
              </Button>
              <Button disabled={savingResources} onClick={saveResources}>
                <CheckIcon />
                {savingResources ? "Saving" : "Save"}
              </Button>
            </div>
          </div>
        ) : resources.length ? (
          resources.map((resource, index) => (
            <ResourceCard key={`${resource.url}:${index}`} resource={resource} />
          ))
        ) : (
          <span className="px-1 text-sm text-muted-foreground">No resources yet.</span>
        )}
      </aside>
    </div>
  );
}

function EditableResource({
  resource,
  onDelete,
}: {
  resource: Resource;
  onDelete: () => void;
}) {
  const Icon =
    resourceIcon[resource.kind as keyof typeof resourceIcon] ?? LinkIcon;
  return (
    <Card className="flex-row items-center gap-3 p-3">
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">
        {resource.title}
      </span>
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={`Delete ${resource.title}`}
        onClick={onDelete}
      >
        <Trash2Icon />
      </Button>
    </Card>
  );
}

// The chat pane, with the coder terminal splitting in on the right once the
// dispatcher launches work. Keyed by chat.id at the call site so useChat
// remounts per chat. The stored transcript loads first: useChat only takes
// initial messages at construction.
function LiveChat({ chat }: { chat: Chat }) {
  const [initialMessages, setInitialMessages] = useState<
    ChatUIMessage[] | null
  >(null);
  const [tasks, setTasks] = useState<CoderTask[]>([]);
  const [showTerminal, setShowTerminal] = useState(false);

  const loadTasks = useCallback(() => {
    fetch(`/api/chats/${chat.id}/tasks`)
      .then((res) => (res.ok ? res.json() : []))
      .then((found: CoderTask[]) => {
        setTasks(found);
        if (found.length) setShowTerminal(true);
      })
      .catch(() => setTasks([]));
  }, [chat.id]);

  useEffect(() => {
    fetch(`/api/chats/${chat.id}/messages`)
      .then((res) => (res.ok ? res.json() : []))
      .then(setInitialMessages)
      .catch(() => setInitialMessages([]));
    loadTasks();
  }, [chat.id, loadTasks]);

  const onMessagesChange = useCallback(
    (messages: ChatUIMessage[]) => {
      const accepted = messages.some((message) =>
        message.parts.some(
          (part) =>
            part.type === "tool-launch_coder" &&
            part.state === "output-available" &&
            !part.preliminary,
        ),
      );
      if (accepted) loadTasks();
    },
    [loadTasks],
  );

  if (initialMessages === null) return <div className="flex-1" />;

  return (
    <div className="@container relative flex min-h-0 flex-1 flex-col @5xl:flex-row">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col @5xl:basis-[28rem]">
        <ChatView
          chatId={chat.id}
          initialMessages={initialMessages}
          onMessagesChange={onMessagesChange}
        />
      </div>
      {tasks.length > 0 && !showTerminal && (
        <Button
          variant="outline"
          size="sm"
          className="absolute top-2 right-2 z-10"
          onClick={() => setShowTerminal(true)}
        >
          <TerminalIcon className="size-4" />
          terminal
        </Button>
      )}
      {showTerminal && tasks.length > 0 && (
        <TerminalPane
          chatId={chat.id}
          tasks={tasks}
          onClose={() => setShowTerminal(false)}
        />
      )}
    </div>
  );
}
