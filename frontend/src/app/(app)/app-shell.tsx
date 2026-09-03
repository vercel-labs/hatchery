"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  ArchiveIcon,
  BookMarkedIcon,
  CheckIcon,
  ChevronsUpDownIcon,
  FolderGitIcon,
  GitBranchIcon,
  LinkIcon,
  TriangleAlertIcon,
  LogOutIcon,
  PencilIcon,
  PlusIcon,
  MessageSquareIcon,
  TerminalIcon,
  TriangleIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  apiBase,
  apiFetch,
  type Chat,
  type Resource,
  type Space,
  type SpaceWarning,
  type User,
  type VercelCLIConnection,
} from "@/lib/api";
import type { ChatUIMessage } from "@/lib/messages";
import { ChatView } from "@/components/chat";
import { SandboxForm } from "@/components/sandbox-form";
import { TerminalPane, type SandboxWorkspace } from "@/components/terminal-pane";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
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

function spaceStripeColor(spaceId: string | null) {
  if (!spaceId) return "var(--muted-foreground)";
  const hue = [...spaceId].reduce((value, char) => value * 31 + char.charCodeAt(0), 0);
  return `hsl(${Math.abs(hue) % 360} 70% 50%)`;
}

function ChatOriginIcon({ trigger }: { trigger: string }) {
  const path = trigger.startsWith("slack:")
    ? "M3.427 10.079c0 .92-.743 1.663-1.663 1.663S.1 10.998.1 10.079c0-.92.743-1.663 1.663-1.663h1.663zm.831 0c0-.92.744-1.663 1.663-1.663.92 0 1.663.743 1.663 1.663v4.157c0 .92-.743 1.663-1.663 1.663s-1.663-.743-1.663-1.663zM5.921 3.402c-.92 0-1.663-.744-1.663-1.663 0-.92.744-1.663 1.663-1.663.92 0 1.663.743 1.663 1.663v1.663zm0 .844c.92 0 1.663.743 1.663 1.663s-.743 1.663-1.663 1.663h-4.17c-.92 0-1.663-.744-1.663-1.663 0-.92.743-1.663 1.663-1.663zM12.586 5.909c0-.92.743-1.663 1.663-1.663s1.663.743 1.663 1.663-.744 1.663-1.663 1.663h-1.663zm-.832 0c0 .92-.743 1.663-1.663 1.663s-1.663-.744-1.663-1.663v-4.17c0-.92.744-1.663 1.663-1.663.92 0 1.663.743 1.663 1.663zM10.091 12.573c.92 0 1.663.743 1.663 1.663s-.743 1.663-1.663 1.663-1.663-.743-1.663-1.663v-1.663zm0-.831c-.92 0-1.663-.744-1.663-1.663 0-.92.744-1.663 1.663-1.663h4.17c.92 0 1.663.743 1.663 1.663s-.743 1.663-1.663 1.663z"
    : trigger.startsWith("github:")
      ? "M8 0C3.58 0 0 3.579 0 7.997a7.99 7.99 0 0 0 5.47 7.588c.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.939-.82-1.129-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.949 0-.87.31-1.589.82-2.149-.08-.2-.36-1.02.08-2.12 0 0 .67-.209 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.039 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.068-1.87 3.748-3.65 3.948.29.25.54.73.54 1.48 0 1.07-.01 1.929-.01 2.199 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 7.997 7.996 7.996 0 0 0 8 0"
      : "M7.157 0 2.333 9.408l-.56 1.092H7a.25.25 0 0 1 .25.25V16h1.593l4.824-9.408.56-1.092H9a.25.25 0 0 1-.25-.25V0zM7 9H4.227L7.25 3.106V5.25C7.25 6.216 8.034 7 9 7h2.773L8.75 12.894V10.75A1.75 1.75 0 0 0 7 9";
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 shrink-0">
      <path fill="currentColor" fillRule="evenodd" d={path} clipRule="evenodd" />
    </svg>
  );
}

function ChatSidebarItem({
  chat,
  selected,
  onArchiveChange,
}: {
  chat: Chat;
  selected: boolean;
  onArchiveChange: (chat: Chat, archived: boolean) => void;
}) {
  const archived = chat.archived_at !== null;
  const name = chat.topic ?? (chat.title === "new chat" ? "…" : chat.title);
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        className="relative pl-3"
        isActive={selected}
        aria-current={selected ? "page" : undefined}
        render={<Link href={`/chats/${encodeURIComponent(chat.id)}`} />}
        tooltip={chat.topic ?? chat.title}
      >
        <span
          className="absolute inset-y-1 left-0 w-0.5 rounded-full"
          style={{ backgroundColor: spaceStripeColor(chat.space_id) }}
        />
        <span
          className={
            chat.status === "running"
              ? "size-2 shrink-0 rounded-full bg-blue-500"
              : "size-2 shrink-0 rounded-full bg-zinc-400"
          }
          title={chat.status === "running" ? "Running" : "Idle"}
        />
        <ChatOriginIcon trigger={chat.trigger} />
        <span className="truncate">{name}</span>
      </SidebarMenuButton>
      <SidebarMenuAction
        showOnHover
        aria-label={`${archived ? "Unarchive" : "Archive"} ${name}`}
        title={archived ? "Unarchive chat" : "Archive chat"}
        onClick={() => onArchiveChange(chat, !archived)}
      >
        <ArchiveIcon />
      </SidebarMenuAction>
    </SidebarMenuItem>
  );
}

export function AppShell() {
  const pathname = usePathname();
  const router = useRouter();
  const spaceMatch = pathname.match(/^\/spaces\/([^/]+)$/);
  const chatMatch = pathname.match(/^\/chats\/([^/]+)$/);
  const selection: Selection = spaceMatch
    ? { kind: "space", id: decodeURIComponent(spaceMatch[1]) }
    : chatMatch
      ? { kind: "chat", id: decodeURIComponent(chatMatch[1]) }
      : null;
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [spaces, setSpaces] = useState<Space[] | null>(null);
  const [chats, setChats] = useState<Chat[] | null>(null);
  const [spaceWarnings, setSpaceWarnings] = useState<SpaceWarning[]>([]);
  const [failed, setFailed] = useState(false);
  const [addingSpace, setAddingSpace] = useState(false);
  const [spaceName, setSpaceName] = useState("");
  const [vercelCLI, setVercelCLI] = useState<VercelCLIConnection | null>(null);
  const [vercelToken, setVercelToken] = useState("");
  const [vercelSheetOpen, setVercelSheetOpen] = useState(false);
  const [vercelError, setVercelError] = useState("");
  const [savingVercel, setSavingVercel] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState<boolean | null>(null);
  // set by space clicks only; chat clicks leave the order alone
  const [sortSpaceId, setSortSpaceId] = useState<string | null>(null);

  const disconnectGitHub = async () => {
    if (!window.confirm("Disconnect GitHub? Active sandboxes will lose repository access.")) {
      return;
    }
    const response = await apiFetch("/api/connections/github", { method: "DELETE" });
    if (response.ok) {
      setUser((current) => current ? { ...current, github: undefined } : current);
    }
  };

  const disconnectSlack = async () => {
    if (!window.confirm("Disconnect Slack? New Slack messages will be ignored.")) return;
    const response = await apiFetch("/api/connections/slack", { method: "DELETE" });
    if (response.ok) {
      setUser((current) => current ? { ...current, slack: undefined } : current);
    }
  };

  const saveVercelCLI = async (event: React.FormEvent) => {
    event.preventDefault();
    setSavingVercel(true);
    setVercelError("");
    const response = await apiFetch("/api/connections/vercel-cli", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: vercelToken }),
    });
    setSavingVercel(false);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      setVercelError(body.detail ?? "Could not connect Vercel CLI access.");
      return;
    }
    const body: { connection: VercelCLIConnection } = await response.json();
    setVercelCLI(body.connection);
    setVercelToken("");
    setVercelSheetOpen(false);
  };

  const disconnectVercelCLI = async () => {
    if (!window.confirm("Disconnect Vercel CLI access? Active sandboxes will lose deployment access.")) {
      return;
    }
    const response = await apiFetch("/api/connections/vercel-cli", { method: "DELETE" });
    if (response.ok) setVercelCLI(null);
  };

  useEffect(() => {
    const load = async () => {
      const identity = await apiFetch("/api/auth/me");
      if (!identity.ok) throw new Error("backend unreachable");
      const me: { user: User | null } = await identity.json();
      if (!me.user) {
        setUser(null);
        return;
      }
      const [s, c, github, slack, vercel, warnings] = await Promise.all([
        apiFetch("/api/spaces"),
        apiFetch("/api/chats"),
        apiFetch("/api/connections/github"),
        apiFetch("/api/connections/slack"),
        apiFetch("/api/connections/vercel-cli"),
        apiFetch("/api/spaces/warnings"),
      ]);
      if (!s.ok || !c.ok || !github.ok || !slack.ok || !vercel.ok || !warnings.ok) {
        throw new Error("backend unreachable");
      }
      const connection: { connection: User["github"] | null } = await github.json();
      const slackConnection: { connection: User["slack"] | null } = await slack.json();
      const vercelConnection: { connection: VercelCLIConnection | null } = await vercel.json();
      setUser({
        ...me.user,
        github: connection.connection ?? undefined,
        slack: slackConnection.connection ?? undefined,
      });
      setVercelCLI(vercelConnection.connection);
      setSpaces(await s.json());
      setChats(await c.json());
      setSpaceWarnings(await warnings.json());
    };
    load().catch(() => setFailed(true));
  }, []);

  const colorOf = (spaceId: string | null) =>
    spaces?.find((s) => s.id === spaceId)?.color;

  const selectedSpace =
    selection?.kind === "space"
      ? (spaces?.find((s) => s.id === selection.id) ?? null)
      : null;
  const selectedChat =
    selection?.kind === "chat"
      ? (chats?.find((c) => c.id === selection.id) ?? null)
      : null;
  const selectedWarning = spaceWarnings.find(
    (warning) =>
      warning.space_id === (selectedSpace?.id ?? selectedChat?.space_id),
  );

  const activeSortSpaceId = selectedSpace?.id ?? sortSpaceId;
  const activeChats = chats?.filter((chat) => chat.archived_at === null) ?? null;
  const sortedChats =
    activeChats && activeSortSpaceId
      ? [...activeChats].sort(
          (a, b) =>
            Number(b.space_id === activeSortSpaceId) -
            Number(a.space_id === activeSortSpaceId),
        )
      : activeChats;
  const archivedChats = chats
    ?.filter((chat) => chat.archived_at !== null)
    .sort((a, b) => (b.archived_at ?? "").localeCompare(a.archived_at ?? "")) ?? [];
  const archiveView = archiveOpen ?? Boolean(selectedChat?.archived_at);

  const createSpace = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!spaceName.trim()) return;
    const res = await apiFetch("/api/spaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: spaceName }),
    });
    if (!res.ok) return;
    const space: Space = await res.json();
    setSpaces((current) => [...(current ?? []), space]);
    router.push(`/spaces/${encodeURIComponent(space.id)}`);
    setSortSpaceId(space.id);
    setSpaceName("");
    setAddingSpace(false);
  };

  const deleteSpace = async (space: Space) => {
    if (!window.confirm(`Remove ${space.name}?`)) return;
    const res = await apiFetch(`/api/spaces/${space.id}`, { method: "DELETE" });
    if (res.status === 409) {
      window.alert("Remove this space's chats first.");
      return;
    }
    if (!res.ok) return;
    setSpaces((current) => current?.filter((item) => item.id !== space.id) ?? null);
    if (selection?.kind === "space" && selection.id === space.id) router.push("/");
    if (sortSpaceId === space.id) setSortSpaceId(null);
  };

  const refreshChats = useCallback(() => {
    apiFetch("/api/chats")
      .then((res) => (res.ok ? res.json() : null))
      .then((found: Chat[] | null) => {
        if (found) setChats(found);
      })
      .catch(() => {});
  }, []);

  const createChat = async () => {
    const res = await apiFetch("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        space_id: selectedSpace?.id ?? sortSpaceId ?? spaces?.[0]?.id,
      }),
    });
    if (!res.ok) return;
    const chat: Chat = await res.json();
    setChats((prev) => [chat, ...(prev ?? [])]);
    router.push(`/chats/${encodeURIComponent(chat.id)}`);
  };

  const setChatArchived = async (chat: Chat, archived: boolean) => {
    const res = await apiFetch(`/api/chats/${chat.id}/archive`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archived }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      window.alert(body.detail ?? `Could not ${archived ? "archive" : "unarchive"} chat.`);
      return;
    }
    const updated: Chat = await res.json();
    setChats((current) =>
      current?.map((item) => (item.id === updated.id ? updated : item)) ?? null,
    );
    if (archived) setArchiveOpen(true);
  };

  const assignChatSpace = async (chat: Chat, spaceId: string) => {
    const res = await apiFetch(`/api/chats/${chat.id}/space`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ space_id: spaceId }),
    });
    if (!res.ok) return;
    const updated: Chat = await res.json();
    setChats((current) =>
      current?.map((item) => (item.id === updated.id ? updated : item)) ?? null,
    );
  };

  if (user === undefined && !failed) return <div className="h-svh" />;

  if (user == null) {
    return (
      <main className="flex h-svh items-center justify-center p-6">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle>Sign in to hatchery</CardTitle>
            <CardDescription>Use your Vercel account to continue.</CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              className="w-full"
              nativeButton={false}
              render={<a href="/api/auth/login" />}
            >
              Sign in with Vercel
            </Button>
          </CardContent>
        </Card>
      </main>
    );
  }

  return (
    <SidebarProvider className="h-svh overflow-hidden">
      <Sidebar>
        <SidebarHeader className="p-2">
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <SidebarMenuButton size="lg" className="h-auto">
                  <Avatar size="sm">
                    <AvatarImage src={user.picture ?? undefined} alt="" />
                    <AvatarFallback>
                      {(user.name ?? user.username ?? user.email ?? "U").slice(0, 1).toUpperCase()}
                    </AvatarFallback>
                  </Avatar>
                  <span className="min-w-0 flex-1">
                    <span className="block font-semibold">hatchery</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {user.name ?? user.username ?? user.email}
                    </span>
                  </span>
                  <ChevronsUpDownIcon />
                </SidebarMenuButton>
              }
            />
            <DropdownMenuContent side="bottom" align="start" className="min-w-60">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Account</DropdownMenuLabel>
                {user.github ? (
                  <DropdownMenuItem disabled>
                    <GitBranchIcon />
                    Connected as @{user.github.login}
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem
                    render={<a href={`${apiBase()}/api/connections/github/authorize`} />}
                  >
                    <GitBranchIcon />
                    Connect GitHub
                  </DropdownMenuItem>
                )}
                {user.slack ? (
                  <DropdownMenuItem disabled>
                    <MessageSquareIcon />
                    {user.slack.user
                      ? `${user.slack.user} in ${user.slack.team ?? user.slack.team_id}`
                      : `Slack connected in ${user.slack.team ?? user.slack.team_id}`}
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem
                    render={<a href={`${apiBase()}/api/connections/slack/authorize`} />}
                  >
                    <MessageSquareIcon />
                    Connect Slack
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onClick={() => setVercelSheetOpen(true)}>
                  <TriangleIcon />
                  {vercelCLI ? "Vercel CLI connected" : "Connect Vercel CLI"}
                </DropdownMenuItem>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                {user.github && (
                  <DropdownMenuItem variant="destructive" onClick={disconnectGitHub}>
                    <GitBranchIcon />
                    Disconnect GitHub
                  </DropdownMenuItem>
                )}
                {user.slack && (
                  <DropdownMenuItem variant="destructive" onClick={disconnectSlack}>
                    <MessageSquareIcon />
                    Disconnect Slack
                  </DropdownMenuItem>
                )}
                {vercelCLI && (
                  <DropdownMenuItem variant="destructive" onClick={disconnectVercelCLI}>
                    <TriangleIcon />
                    Disconnect Vercel CLI
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  onClick={async () => {
                    await apiFetch("/api/auth/logout", { method: "POST" });
                    window.location.reload();
                  }}
                >
                  <LogOutIcon />
                  Sign out
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </SidebarHeader>
        <SidebarSeparator />

        <SidebarContent>
          {archiveView ? (
            <SidebarGroup aria-label="Archived chats">
              <SidebarGroupLabel>Archive</SidebarGroupLabel>
              <SidebarGroupAction
                title="Close archive"
                aria-label="Close archive"
                onClick={() => setArchiveOpen(false)}
              >
                <XIcon />
              </SidebarGroupAction>
              <SidebarGroupContent>
                <SidebarMenu>
                  {archivedChats.length > 0 ? (
                    archivedChats.map((chat) => (
                      <ChatSidebarItem
                        key={chat.id}
                        chat={chat}
                        selected={selectedChat?.id === chat.id}
                        onArchiveChange={(item, archived) =>
                          void setChatArchived(item, archived)
                        }
                      />
                    ))
                  ) : (
                    <li className="px-2 py-4 text-sm text-muted-foreground">
                      No archived chats
                    </li>
                  )}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ) : (
            <>
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
                              onClick={() => setSortSpaceId(space.id)}
                              render={
                                <Link href={`/spaces/${encodeURIComponent(space.id)}`} />
                              }
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
                          <ChatSidebarItem
                            key={chat.id}
                            chat={chat}
                            selected={selectedChat?.id === chat.id}
                            onArchiveChange={(item, archived) =>
                              void setChatArchived(item, archived)
                            }
                          />
                        ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>
            </>
          )}
        </SidebarContent>
        {!archiveView && (
          <SidebarFooter className="border-t border-sidebar-border">
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  aria-label={`Open archive, ${archivedChats.length} chats`}
                  onClick={() => setArchiveOpen(true)}
                  tooltip="Archive"
                >
                  <ArchiveIcon />
                  <span>Archive</span>
                  {archivedChats.length > 0 && (
                    <span className="ml-auto tabular-nums">{archivedChats.length}</span>
                  )}
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarFooter>
        )}
      </Sidebar>

      <Sheet open={vercelSheetOpen} onOpenChange={setVercelSheetOpen}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>{vercelCLI ? "Replace Vercel CLI token" : "Connect Vercel CLI"}</SheetTitle>
            <SheetDescription>
              Add your Vercel token to let agents access the teams and projects you can access.
            </SheetDescription>
          </SheetHeader>
          <form className="flex flex-col gap-4 px-4" onSubmit={saveVercelCLI}>
            <FieldGroup>
              <Field data-invalid={Boolean(vercelError)}>
                <FieldLabel htmlFor="vercel-token">Access token</FieldLabel>
                <Input
                  id="vercel-token"
                  type="password"
                  autoComplete="off"
                  value={vercelToken}
                  onChange={(event) => setVercelToken(event.target.value)}
                  aria-invalid={Boolean(vercelError)}
                  placeholder="vcp_…"
                />
                <FieldDescription>
                  Create the narrowest token possible in Vercel account settings. It is stored encrypted.
                </FieldDescription>
                {vercelError && <FieldError>{vercelError}</FieldError>}
              </Field>
            </FieldGroup>
            <Button type="submit" disabled={!vercelToken.trim() || savingVercel}>
              {savingVercel ? "Connecting…" : vercelCLI ? "Replace token" : "Connect"}
            </Button>
          </form>
          <SheetFooter>
            <Button
              variant="outline"
              nativeButton={false}
              render={<a href="https://vercel.com/account/settings/tokens" target="_blank" rel="noreferrer" />}
            >
              Create token in Vercel
            </Button>
          </SheetFooter>
        </SheetContent>
      </Sheet>

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
          <span className="min-w-0 flex-1 truncate text-sm font-medium">
            {selectedSpace?.name ?? selectedChat?.title ?? "hatchery"}
          </span>
          {selectedChat?.archived_at && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void setChatArchived(selectedChat, false)}
            >
              <ArchiveIcon />
              Unarchive
            </Button>
          )}
          {selectedChat?.space_id && spaces && (
            <Select
              value={selectedChat.space_id}
              onValueChange={(spaceId) => {
                if (spaceId) void assignChatSpace(selectedChat, spaceId);
              }}
            >
              <SelectTrigger size="sm" aria-label="Chat space">
                <SelectValue placeholder="Assign space" />
              </SelectTrigger>
              <SelectContent align="end">
                <SelectGroup>
                  {spaces.map((space) => (
                    <SelectItem key={space.id} value={space.id}>
                      <Dot color={space.color} />
                      {space.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          )}
        </header>
        {selectedChat && !failed ? (
          <LiveChat
            key={selectedChat.id}
            chat={selectedChat}
            warning={selectedWarning?.warning}
            onChatChanged={refreshChats}
            onUnarchive={() => void setChatArchived(selectedChat, false)}
            onSpaceAssigned={(spaceId) =>
              setChats((current) => {
                if (
                  current?.find((chat) => chat.id === selectedChat.id)
                    ?.space_id === spaceId
                ) {
                  return current;
                }
                return (
                  current?.map((chat) =>
                    chat.id === selectedChat.id
                      ? { ...chat, space_id: spaceId }
                      : chat,
                  ) ?? null
                );
              })
            }
          />
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
                warning={selectedWarning?.warning}
                onChange={(updated) =>
                  setSpaces((current) =>
                    current?.map((space) =>
                      space.id === updated.id ? updated : space,
                    ) ?? null,
                  )
                }
              />
            ) : selection && spaces !== null && chats !== null ? (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Not found</EmptyTitle>
                  <EmptyDescription>
                    This {selection.kind} does not exist or you cannot access it.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
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

function RepositoryWarning({ warning }: { warning: string }) {
  return (
    <Alert>
      <TriangleAlertIcon />
      <AlertTitle>GitHub access needed</AlertTitle>
      <AlertDescription>{warning}</AlertDescription>
    </Alert>
  );
}

function SpacePane({
  space,
  warning,
  onChange,
}: {
  space: Space;
  warning?: string;
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
      const response = await apiFetch(`/api/spaces/${space.id}`, {
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
      const response = await apiFetch(`/api/spaces/${space.id}/resources`, {
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
        {warning && <RepositoryWarning warning={warning} />}
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

// Keyed by chat.id at the call site so useChat remounts per chat.
function LiveChat({
  chat,
  warning,
  onChatChanged,
  onUnarchive,
  onSpaceAssigned,
}: {
  chat: Chat;
  warning?: string;
  onChatChanged: () => void;
  onUnarchive: () => void;
  onSpaceAssigned: (spaceId: string) => void;
}) {
  const [initialMessages, setInitialMessages] = useState<
    ChatUIMessage[] | null
  >(null);
  const [sandboxes, setSandboxes] = useState<SandboxWorkspace[]>([]);
  const [messageRevision, setMessageRevision] = useState(0);
  const [streamGeneration, setStreamGeneration] = useState(0);
  const [showTerminal, setShowTerminal] = useState(false);
  const [showSandboxForm, setShowSandboxForm] = useState(false);
  const [preferredSandboxId, setPreferredSandboxId] = useState<string>();

  const loadSandboxes = useCallback(() => {
    apiFetch(`/api/chats/${chat.id}/sandboxes`)
      .then((res) => (res.ok ? res.json() : []))
      .then((found: SandboxWorkspace[]) => {
        setSandboxes(found);
        if (found.length) setShowTerminal(true);
      })
      .catch(() => setSandboxes([]));
  }, [chat.id]);

  useEffect(() => {
    apiFetch(`/api/chats/${chat.id}/messages`)
      .then((res) => (res.ok ? res.json() : []))
      .then(setInitialMessages)
      .catch(() => setInitialMessages([]));
    loadSandboxes();
  }, [chat.id, loadSandboxes]);

  useEffect(() => {
    const source = new EventSource(
      `${apiBase()}/api/chats/${chat.id}/events`,
      { withCredentials: true },
    );
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as {
        type?: string;
        generation?: number;
      };
      if (event.type === "chat.changed" || event.type === "task.changed") {
        onChatChanged();
      }
      if (event.type === "task.changed" || event.type === "sandbox.changed") {
        loadSandboxes();
      }
      if (event.type === "messages.changed") {
        setMessageRevision((revision) => revision + 1);
      }
      if (
        event.type === "stream.available" &&
        typeof event.generation === "number"
      ) {
        const announcedGeneration = event.generation + 1;
        setStreamGeneration((generation) =>
          Math.max(generation, announcedGeneration),
        );
      }
    };
    return () => source.close();
  }, [chat.id, loadSandboxes, onChatChanged]);

  const onMessagesChange = useCallback(
    (messages: ChatUIMessage[]) => {
      const assignment = messages
        .flatMap((message) => message.parts)
        .findLast(
          (part) =>
            part.type === "data-space-assignment" &&
            part.data.state === "assigned",
        );
      if (
        assignment?.type === "data-space-assignment" &&
        assignment.data.space_id
      ) {
        onSpaceAssigned(assignment.data.space_id);
      }
    },
    [onSpaceAssigned],
  );

  if (initialMessages === null) return <div className="flex-1" />;

  return (
    <div className="@container flex min-h-0 flex-1">
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col @4xl:flex-row">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col @4xl:min-w-[28rem]">
          {warning && (
            <div className="p-3 pb-0">
              <RepositoryWarning warning={warning} />
            </div>
          )}
          <ChatView
            chatId={chat.id}
            initialMessages={initialMessages}
            spaceId={chat.space_id}
            messageRevision={messageRevision}
            streamGeneration={streamGeneration}
            archived={chat.archived_at !== null}
            onMessagesChange={onMessagesChange}
            onUnarchive={onUnarchive}
            onCreateSandbox={() => setShowSandboxForm(true)}
          />
        </div>
        {sandboxes.length > 0 && !showTerminal && (
          <Button
            variant="outline"
            size="sm"
            className="absolute top-2 right-2"
            onClick={() => setShowTerminal(true)}
          >
            <TerminalIcon />
            terminal
          </Button>
        )}
        {showTerminal && sandboxes.length > 0 && (
          <TerminalPane
            key={`${chat.id}:${preferredSandboxId ?? ""}`}
            chatId={chat.id}
            sandboxes={sandboxes}
            preferredSandboxId={preferredSandboxId}
            onClose={() => setShowTerminal(false)}
            onCreateSandbox={() => setShowSandboxForm(true)}
            onChanged={loadSandboxes}
          />
        )}
        <SandboxForm
          chatId={chat.id}
          open={showSandboxForm}
          onOpenChange={setShowSandboxForm}
          onCreated={(sandboxId) => {
            setPreferredSandboxId(sandboxId);
            setShowTerminal(true);
            loadSandboxes();
          }}
        />
      </div>
    </div>
  );
}
