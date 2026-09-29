import { useLocation, useNavigate } from "@tanstack/react-router";
import { Activity, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArchiveIcon,
  BracesIcon,
  CheckIcon,
  ChevronsUpDown,
  FolderOpenIcon,
  FolderTreeIcon,
  GitBranchIcon,
  LogOutIcon,
  MessageSquareIcon,
  PlusIcon,
  XIcon,
} from "lucide-react";

import {
  apiFetch,
  type Agent,
  type AgentWarning,
  type Chat,
  type User,
} from "@/lib/api";
import type { AgentThreads } from "@/lib/api-types";
import { chatSidebarText, sidebarThreads } from "@/lib/chat-sidebar";
import { type AccentColor } from "@/lib/agent-colors";
import { useApi } from "@/hooks/use-api";
import { AgentSwitcher } from "@/app/agent-switcher";
import { ConsoleLayoutSkeleton } from "@/app/console-layout-skeleton";
import {
  createChatPersister,
  newChatId,
  startsFreshDraft,
  type NewChatRequest,
} from "@/components/new-chat-state";
import { AgentColorPicker } from "@/components/agent-color-picker";
import { ChatOriginIcon } from "@/components/chat-origin-icon";
import { ResizeHandle } from "@/components/resize-handle";
import { AgentApiView } from "@/features/agents/agent-api-view";
import { AgentPage } from "@/features/agents/agent-page";
import { BudgetHoldNotice } from "@/features/agents/budget-hold-notice";
import { RepositoryView } from "@/features/repository/repository-view";
import { chatViews, Conversation, type ChatView } from "@/features/threads/conversation";
import { ThreadNavigation } from "@/features/threads/thread-navigation";
import { threadsWithObjectives } from "@/features/threads/thread-tree";
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
import { Input } from "@/components/ui/input";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";

// The main pane shows one context: an agent view (picked in the sidebar menu)
// or a chat with its tabs.
type AgentView = "workspace" | "files" | "api";
type Selection =
  | { kind: "agent"; id: string; view: AgentView }
  | { kind: "chat"; id: string; view: ChatView }
  | { kind: "repository" }
  | null;

const AGENT_KEY = "hatchery:agent";
const agentMenu = [
  { view: "workspace", label: "Workspace", icon: FolderOpenIcon, to: "/agents/$agentId" },
  { view: "files", label: "Files", icon: FolderTreeIcon, to: "/agents/$agentId/files" },
  { view: "api", label: "API", icon: BracesIcon, to: "/agents/$agentId/api" },
] as const;

function parseSelection(pathname: string): Selection {
  const agent = pathname.match(/^\/agents\/([^/]+)(?:\/(files|api))?$/);
  if (agent)
    return {
      kind: "agent",
      id: decodeURIComponent(agent[1]),
      view: (agent[2] as AgentView | undefined) ?? "workspace",
    };
  const chat = pathname.match(/^\/chats\/([^/]+)(?:\/([^/]+))?$/);
  if (chat)
    return {
      kind: "chat",
      id: decodeURIComponent(chat[1]),
      view: chatViews.find((view) => view === chat[2]) ?? "chat",
    };
  if (pathname === "/repository") return { kind: "repository" };
  return null;
}

// Every roster, for the Repository's proposal list across agents.
function AllRosters({
  agents,
  children,
}: {
  agents: Agent[];
  children: (rosters: AgentThreads[], refresh: () => Promise<unknown>) => React.ReactNode;
}) {
  const [rosters, setRosters] = useState<AgentThreads[]>([]);
  const load = useCallback(async () => {
    const found = await Promise.all(
      agents.map(async (agent) => {
        const response = await apiFetch(
          `/api/agents/${encodeURIComponent(agent.id)}/threads`,
        ).catch(() => null);
        return response?.ok ? ((await response.json()) as AgentThreads) : null;
      }),
    );
    setRosters(found.filter((item): item is AgentThreads => item !== null));
  }, [agents]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => void load());
    return () => cancelAnimationFrame(frame);
  }, [load]);
  return <>{children(rosters, load)}</>;
}

// Keeps navigation widths on the sidebar wrapper, like agentmesh's layout.
function SidebarResize({
  containerRef,
  paneRef,
}: {
  containerRef: React.RefObject<HTMLDivElement | null>;
  paneRef: React.RefObject<HTMLDivElement | null>;
}) {
  const { state } = useSidebar();
  if (state === "collapsed") return null;
  return (
    <ResizeHandle
      containerRef={containerRef}
      paneRef={paneRef}
      variable="--sidebar-width"
      direction={1}
      label="Resize navigation"
      minimum={208}
      maximum={416}
      minimumContent={480}
      defaultValue={256}
      className="-ml-px hidden md:block"
    />
  );
}

// Agent views have no top bar; this one only brings a hidden sidebar back.
function SidebarReopen() {
  const { state, isMobile } = useSidebar();
  if (state === "expanded" && !isMobile) return null;
  return (
    <div className="flex h-12 shrink-0 items-center border-b px-3">
      <SidebarTrigger />
    </div>
  );
}

export function AppShell() {
  const pathname = useLocation({ select: (location) => location.pathname });
  const navigate = useNavigate();
  const selection = parseSelection(pathname);
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [chats, setChats] = useState<Chat[] | null>(null);
  const [childChats, setChildChats] = useState<Chat[]>([]);
  const [agentWarnings, setAgentWarnings] = useState<AgentWarning[]>([]);
  const [failed, setFailed] = useState(false);
  const [agentName, setAgentName] = useState("");
  const [agentColor, setAgentColor] = useState<AccentColor | null>(null);
  const [addingAgent, setAddingAgent] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [preferredAgent, setPreferredAgent] = useState<string | null>(() =>
    typeof localStorage === "undefined" ? null : localStorage.getItem(AGENT_KEY),
  );
  // A draft keeps one conversation mounted from the first keystroke through
  // chat creation, so its message and any follow-up draft survive.
  const [draft, setDraft] = useState(() => ({ key: 0, chatId: newChatId() }));
  const [draftAgent, setDraftAgent] = useState<string | null>(null);
  const [composeRequest, setComposeRequest] = useState(0);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const previousPath = useRef<string | null>(null);
  const draftPersister = useRef<((agentId: string | null) => Promise<Chat>) | null>(
    null,
  );
  const layoutRef = useRef<HTMLDivElement>(null);
  // The last conversation stays mounted (hidden) under other views, so its
  // draft and stream survive switching to Workspace, API, or Repository.
  const [kept, setKept] = useState<{ draft: boolean; chatId: string } | null>(null);
  const navigationRef = useRef<HTMLDivElement>(null);

  const allChats = useMemo(() => [...(chats ?? []), ...childChats], [chats, childChats]);
  const routedChat =
    selection?.kind === "chat"
      ? (allChats.find((chat) => chat.id === selection.id) ?? null)
      : null;
  const agentId =
    selection?.kind === "agent"
      ? selection.id
      : (routedChat?.agent_id ??
        (agents?.some((agent) => agent.id === preferredAgent)
          ? preferredAgent
          : (agents?.[0]?.id ?? null)));
  const agent = agents?.find((item) => item.id === agentId);

  // Remember the agent of the last visited page for the next new thread.
  if (agentId && agentId !== preferredAgent) setPreferredAgent(agentId);
  useEffect(() => {
    if (agentId) localStorage.setItem(AGENT_KEY, agentId);
  }, [agentId]);

  const draftPersisted = allChats.some((chat) => chat.id === draft.chatId);
  useEffect(() => {
    // Coming back to "/" keeps an unsent draft; a sent one starts over.
    if (startsFreshDraft(previousPath.current, pathname) && draftPersisted) {
      draftPersister.current = null;
      setDraft((current) => ({ key: current.key + 1, chatId: newChatId() }));
      setDraftAgent(agentId);
    }
    previousPath.current = pathname;
    // agentId is read once, when a fresh draft starts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  useEffect(() => {
    if (composeRequest) composerRef.current?.focus();
  }, [composeRequest]);

  useEffect(() => {
    const load = async () => {
      const identity = await apiFetch("/api/auth/me");
      if (!identity.ok) throw new Error("backend unreachable");
      const me: { user: User | null } = await identity.json();
      if (!me.user) {
        setUser(null);
        return;
      }
      const [s, c, github, slack, warnings] = await Promise.all([
        apiFetch("/api/agents"),
        apiFetch("/api/chats"),
        apiFetch("/api/connections/github"),
        apiFetch("/api/connections/slack"),
        apiFetch("/api/agents/warnings"),
      ]);
      if (!s.ok || !c.ok || !github.ok || !slack.ok || !warnings.ok) {
        throw new Error("backend unreachable");
      }
      const connection: { connection: User["github"] | null } = await github.json();
      const slackConnection: { connection: User["slack"] | null } = await slack.json();
      setUser({
        ...me.user,
        github: connection.connection ?? undefined,
        slack: slackConnection.connection ?? undefined,
      });
      setAgents(await s.json());
      setChats(await c.json());
      setAgentWarnings(await warnings.json());
    };
    load().catch(() => {
      setFailed(true);
      setUser((current) => current ?? null);
    });
  }, []);

  // Other threads' activity has no stream in Hatchery; the roster polls.
  const { data: roster, mutate: mutateRoster } = useApi<AgentThreads>(
    user && agentId ? `/api/agents/${encodeURIComponent(agentId)}/threads` : null,
    5_000,
    { keepPreviousData: true },
  );
  const refreshRoster = useCallback(() => mutateRoster(), [mutateRoster]);
  const rosterThreads = useMemo(
    () => (roster?.agent_id === agentId ? threadsWithObjectives(roster) : []),
    [agentId, roster],
  );
  const threads = useMemo(
    () => sidebarThreads(chats ?? [], rosterThreads, agentId),
    [agentId, chats, rosterThreads],
  );

  // A delegated thread's chat is listed under its parent chat.
  useEffect(() => {
    if (selection?.kind !== "chat" || routedChat || !chats) return;
    const child = rosterThreads.find((thread) => thread.chat_id === selection.id);
    const parent = rosterThreads.find(
      (thread) => thread.thread_id === child?.parent_thread_id,
    );
    if (!parent?.chat_id) return;
    apiFetch(`/api/chats?parent_chat_id=${encodeURIComponent(parent.chat_id)}`)
      .then((response) => (response.ok ? response.json() : []))
      .then((found: Chat[]) =>
        setChildChats((current) => [
          ...current.filter((chat) => !found.some((item) => item.id === chat.id)),
          ...found,
        ]),
      )
      .catch(() => {});
  }, [chats, rosterThreads, routedChat, selection]);

  // A sent message wakes its sleeping card until the roster reports the sandbox.
  const [wakes, setWakes] = useState<string[]>([]);
  const activeChats = new Set(
    rosterThreads
      .filter((thread) => thread.activity?.sandbox_active)
      .map((thread) => thread.chat_id),
  );
  if (wakes.some((chatId) => activeChats.has(chatId)))
    setWakes(wakes.filter((chatId) => !activeChats.has(chatId)));
  const optimisticallyAwake = new Set(
    threads
      .filter((thread) => thread.chat_id && wakes.includes(thread.chat_id))
      .map((thread) => thread.thread_id),
  );

  const threadCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const chat of chats ?? [])
      if (chat.agent_id && chat.archived_at === null)
        counts[chat.agent_id] = (counts[chat.agent_id] ?? 0) + 1;
    return counts;
  }, [chats]);
  const archivedChats =
    chats
      ?.filter((chat) => chat.archived_at !== null)
      .sort((a, b) => (b.archived_at ?? "").localeCompare(a.archived_at ?? "")) ?? [];
  const selectedWarning = agentWarnings.find(
    (warning) => warning.agent_id === (routedChat?.agent_id ?? agentId),
  )?.warning;

  const disconnectGitHub = async () => {
    if (!window.confirm("Disconnect GitHub? Active sandboxes will lose repository access.")) {
      return;
    }
    const response = await apiFetch("/api/connections/github", { method: "DELETE" });
    if (response.ok) setUser((current) => (current ? { ...current, github: undefined } : current));
  };

  const disconnectSlack = async () => {
    if (!window.confirm("Disconnect Slack? New Slack messages will be ignored.")) return;
    const response = await apiFetch("/api/connections/slack", { method: "DELETE" });
    if (response.ok) setUser((current) => (current ? { ...current, slack: undefined } : current));
  };

  const createAgent = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!agentName.trim()) return;
    const res = await apiFetch("/api/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: agentName,
        ...(agentColor ? { color: agentColor } : {}),
      }),
    });
    if (res.status === 409) {
      window.alert("An agent with this ID already exists. Pick another name.");
      return;
    }
    if (res.status === 422) {
      window.alert("Agent names need at least one letter or digit.");
      return;
    }
    if (!res.ok) return;
    const created: Agent = await res.json();
    setAgents((current) => [...(current ?? []), created]);
    setAgentName("");
    setAgentColor(null);
    setAddingAgent(false);
    openNewChat(created.id);
  };

  // Deleting stops the agent's threads and archives its chats; they stay in Archive.
  const deleteAgent = async (target: Agent) => {
    if (
      !window.confirm(
        `Delete ${target.name}? Its threads stop and its chats move to the archive.`,
      )
    )
      return;
    const res = await apiFetch(`/api/agents/${encodeURIComponent(target.id)}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      window.alert(body.detail ?? `Could not delete ${target.name}.`);
      return;
    }
    const rest = agents?.filter((item) => item.id !== target.id) ?? [];
    setAgents(rest);
    void refreshChats();
    if (target.id === agentId) {
      if (rest[0]) setPreferredAgent(rest[0].id);
      openNewChat(rest[0]?.id ?? null);
    }
  };

  const refreshingChats = useRef(false);
  const refreshChatsAgain = useRef(false);
  const refreshChats = useCallback(async () => {
    if (refreshingChats.current) {
      refreshChatsAgain.current = true;
      return;
    }
    refreshingChats.current = true;
    do {
      refreshChatsAgain.current = false;
      try {
        const response = await apiFetch("/api/chats");
        const found: Chat[] | null = response.ok ? await response.json() : null;
        if (found) setChats(found);
      } catch {}
    } while (refreshChatsAgain.current);
    refreshingChats.current = false;
  }, []);

  const persistDraftChat = useCallback(
    async (nextAgentId: string | null) => {
      if (!draftPersister.current) {
        draftPersister.current = createChatPersister(async (request: NewChatRequest) => {
          const response = await apiFetch("/api/chats", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(request),
          });
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail ?? "Could not create chat");
          }
          const chat: Chat = await response.json();
          setChats((current) => [chat, ...(current ?? []).filter((item) => item.id !== chat.id)]);
          return chat;
        }, draft.chatId);
      }
      const chat = await draftPersister.current(nextAgentId);
      if (window.location.pathname === "/") {
        void navigate({ to: "/chats/$chatId", params: { chatId: chat.id }, replace: true });
      }
      return chat;
    },
    [draft.chatId, navigate],
  );

  // New thread: an untouched draft is kept and focused rather than replaced.
  function openNewChat(nextAgentId: string | null = agentId) {
    if (draftPersisted) {
      draftPersister.current = null;
      setDraft((current) => ({ key: current.key + 1, chatId: newChatId() }));
    }
    setDraftAgent(nextAgentId);
    setComposeRequest((value) => value + 1);
    if (pathname !== "/") void navigate({ to: "/" });
  }

  const openThread = (threadId: string) => {
    const thread = [...threads, ...rosterThreads].find((item) => item.thread_id === threadId);
    if (thread?.chat_id) void navigate({ to: "/chats/$chatId", params: { chatId: thread.chat_id } });
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
    updateChat(updated);
    void refreshRoster();
  };

  const updateChat = useCallback((updated: Chat) => {
    setChats((current) => current?.map((item) => (item.id === updated.id ? updated : item)) ?? null);
    setChildChats((current) => current.map((item) => (item.id === updated.id ? updated : item)));
  }, []);

  const assignChatAgent = async (chat: Chat, nextAgentId: string) => {
    const res = await apiFetch(`/api/chats/${chat.id}/agent`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: nextAgentId }),
    });
    if (!res.ok) return;
    updateChat(await res.json());
  };

  const onAgentAssigned = useCallback(
    (assigned: string) =>
      setChats((current) => {
        const target = current?.find((chat) => chat.id === routedChat?.id);
        if (!target || target.agent_id === assigned) return current;
        return current?.map((chat) => (chat.id === target.id ? { ...chat, agent_id: assigned } : chat)) ?? null;
      }),
    [routedChat?.id],
  );

  if (user === undefined) return <ConsoleLayoutSkeleton />;

  if (user === null && !failed) {
    return (
      <main className="flex h-svh items-center justify-center p-6">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle>Sign in to hatchery</CardTitle>
            <CardDescription>Use your Vercel account to continue.</CardDescription>
          </CardHeader>
          <CardContent>
            <Button className="w-full" nativeButton={false} render={<a href="/api/auth/login" />}>
              Sign in with Vercel
            </Button>
          </CardContent>
        </Card>
      </main>
    );
  }

  const leading = <SidebarTrigger />;
  const userName = user?.name ?? user?.username ?? user?.email ?? "hatchery";
  const draftSelected =
    selection === null || (selection.kind === "chat" && selection.id === draft.chatId);
  const onConversation = !failed && (draftSelected || routedChat !== null);
  const current = onConversation
    ? { draft: draftSelected, chatId: draftSelected ? draft.chatId : routedChat!.id }
    : null;
  if (current && (kept?.draft !== current.draft || kept.chatId !== current.chatId))
    setKept(current);
  const shown = current ?? kept;
  const shownId = shown?.draft ? draft.chatId : shown?.chatId;
  const shownChat = allChats.find((chat) => chat.id === shownId) ?? null;

  let content: React.ReactNode;
  if (failed) {
    content = (
      <div className="flex flex-1 items-center justify-center p-6">
        <Empty>
          <EmptyHeader>
            <EmptyTitle>Backend unreachable</EmptyTitle>
            <EmptyDescription>
              Could not load agents and chats. Reload the page.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </div>
    );
  } else if (selection?.kind === "repository") {
    content = agents ? (
      <AllRosters agents={agents}>
        {(rosters, refresh) => (
          <RepositoryView
            rosters={rosters}
            refresh={refresh}
          />
        )}
      </AllRosters>
    ) : null;
  } else if (selection?.kind === "agent" && agent) {
    const view = selection.view;
    content = (
      <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label={agent.name}>
        <SidebarReopen />
        {view === "api" ? (
          <AgentApiView key={agent.id} agentId={agent.id} />
        ) : (
          <AgentPage
            key={agent.id}
            agent={agent}
            view={view === "files" ? "files" : "overview"}
            roster={roster?.agent_id === agent.id ? roster : undefined}
            warning={selectedWarning}
            refreshRoster={refreshRoster}
            onChange={(updated) =>
              setAgents((current) => current?.map((item) => (item.id === updated.id ? updated : item)) ?? null)
            }
            onRemove={() => void deleteAgent(agent)}
          />
        )}
      </section>
    );
  } else if (onConversation) {
    content = null;
  } else if (agents !== null && chats !== null) {
    content = (
      <div className="flex flex-1 items-center justify-center p-6">
        <Empty>
          <EmptyHeader>
            <EmptyTitle>Not found</EmptyTitle>
            <EmptyDescription>This page does not exist or you cannot access it.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      </div>
    );
  }

  const conversation =
    shown && shownId && (shown.draft || shownChat) ? (
      <Activity mode={onConversation ? "visible" : "hidden"}>
        <Conversation
          key={shown.draft ? `draft:${draft.key}` : shownId}
          chatId={shownId}
          chat={shownChat}
          agents={agents ?? []}
          agentId={shownChat ? shownChat.agent_id : draftAgent}
          roster={roster?.agent_id === (shownChat?.agent_id ?? draftAgent) ? roster : undefined}
          warning={selectedWarning}
          leading={leading}
          view={selection?.kind === "chat" ? selection.view : "chat"}
          onViewChange={(view) => {
            if (view === "chat") void navigate({ to: "/chats/$chatId", params: { chatId: shownId } });
            else void navigate({ to: "/chats/$chatId/$view", params: { chatId: shownId, view } });
          }}
          composerRef={composerRef}
          onPersist={persistDraftChat}
          refreshRoster={refreshRoster}
          onChatChanged={refreshChats}
          onChatUpdated={updateChat}
          onAgentChange={(next) =>
            shownChat ? assignChatAgent(shownChat, next) : setDraftAgent(next)
          }
          onArchiveChange={(archived) => shownChat && void setChatArchived(shownChat, archived)}
          onAgentAssigned={onAgentAssigned}
          onThread={openThread}
          onNewThread={() => openNewChat()}
          onSent={() => setWakes((items) => [...items, shownId])}
        />
      </Activity>
    ) : null;

  const selectedThread = routedChat
    ? threads.find((thread) => thread.chat_id === routedChat.id)?.thread_id ??
      rosterThreads.find((thread) => thread.chat_id === routedChat.id)?.thread_id ??
      null
    : null;

  return (
    <SidebarProvider ref={layoutRef} className="h-svh overflow-hidden">
      <Sidebar ref={navigationRef} aria-label="Console navigation">
        <SidebarHeader className="gap-2 p-2">
          {agent && agents ? (
            <AgentSwitcher
              agents={agents}
              agent={agent}
              threadCounts={threadCounts}
              onSelect={(id) => {
                const latest = (chats ?? [])
                  .filter((chat) => chat.agent_id === id && chat.archived_at === null)
                  .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
                setPreferredAgent(id);
                localStorage.setItem(AGENT_KEY, id);
                if (latest) void navigate({ to: "/chats/$chatId", params: { chatId: latest.id } });
                else openNewChat(id);
              }}
              onAdd={() => setAddingAgent(true)}
              onDelete={(target) => void deleteAgent(target)}
            />
          ) : null}
          {addingAgent || (agents !== null && !agents.length) ? (
            <form
              className="flex flex-col gap-2 rounded-xl border p-2"
              aria-label="New agent"
              onSubmit={createAgent}
            >
              <div className="flex gap-1">
                <Input
                  autoFocus
                  value={agentName}
                  onChange={(event) => setAgentName(event.target.value)}
                  placeholder="New agent name"
                  aria-label="Agent name"
                  className="h-7"
                />
                <Button type="submit" size="icon-xs" disabled={!agentName.trim()}>
                  <CheckIcon />
                  <span className="sr-only">Add agent</span>
                </Button>
                {agents?.length ? (
                  <Button
                    type="button"
                    size="icon-xs"
                    variant="ghost"
                    onClick={() => {
                      setAddingAgent(false);
                      setAgentName("");
                      setAgentColor(null);
                    }}
                  >
                    <XIcon />
                    <span className="sr-only">Cancel</span>
                  </Button>
                ) : null}
              </div>
              <AgentColorPicker value={agentColor} onValueChange={setAgentColor} allowUnselected />
            </form>
          ) : null}
          {agent ? (
            <nav aria-label={`${agent.name} agent`}>
              <SidebarMenu>
                {agentMenu.map((item) => {
                  const active =
                    selection?.kind === "agent" && selection.view === item.view;
                  return (
                    <SidebarMenuItem key={item.view}>
                      <SidebarMenuButton
                        isActive={active}
                        aria-current={active ? "page" : undefined}
                        onClick={() =>
                          void navigate({ to: item.to, params: { agentId: agent.id } })
                        }
                      >
                        <item.icon />
                        <span>{item.label}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </nav>
          ) : null}
          <div className="flex h-7 items-center gap-1 px-2 pt-1">
            <DropdownMenu>
              <DropdownMenuTrigger
                className="-ms-1.5 flex h-6 items-center gap-1 rounded-md px-1.5 text-xs font-medium text-sidebar-foreground/70 outline-none transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground focus-visible:ring-3 focus-visible:ring-ring/50 data-popup-open:bg-sidebar-accent"
                aria-label={`Showing ${archiveOpen ? "archived chats" : "chats"}. Switch list`}
              >
                {archiveOpen ? "Archive" : "Chats"}
                <ChevronsUpDown className="size-3" aria-hidden />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" sideOffset={4} className="min-w-36">
                {[false, true].map((archive) => (
                  <DropdownMenuItem key={String(archive)} onClick={() => setArchiveOpen(archive)}>
                    <span className="flex-1">{archive ? "Archive" : "Chats"}</span>
                    {archive && archivedChats.length ? (
                      <span className="tabular-nums text-xs text-muted-foreground">
                        {archivedChats.length}
                      </span>
                    ) : null}
                    {archiveOpen === archive ? (
                      <CheckIcon aria-label="Selected" />
                    ) : null}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
            {!archiveOpen ? (
              <Button
                size="icon-xs"
                variant="ghost"
                className="ml-auto"
                aria-label="New thread"
                title="New thread"
                onClick={() => openNewChat()}
              >
                <PlusIcon />
              </Button>
            ) : null}
          </div>
        </SidebarHeader>

        <SidebarContent className="gap-0">
          {archiveOpen ? (
            <SidebarGroup aria-label="Archived chats">
              <SidebarGroupContent>
                <SidebarMenu>
                  {archivedChats.length ? (
                    archivedChats.map((chat) => {
                      const text = chatSidebarText(chat);
                      return (
                        <SidebarMenuItem key={chat.id}>
                          <SidebarMenuButton
                            isActive={routedChat?.id === chat.id}
                            onClick={() => void navigate({ to: "/chats/$chatId", params: { chatId: chat.id } })}
                            tooltip={text.label}
                          >
                            <ChatOriginIcon trigger={chat.trigger} />
                            <span className="truncate">{text.label}</span>
                          </SidebarMenuButton>
                          <SidebarMenuAction
                            showOnHover
                            aria-label={`Unarchive ${text.label}`}
                            title="Unarchive chat"
                            onClick={() => void setChatArchived(chat, false)}
                          >
                            <ArchiveIcon />
                          </SidebarMenuAction>
                        </SidebarMenuItem>
                      );
                    })
                  ) : (
                    <li className="px-2 py-4 text-sm text-muted-foreground">No archived chats</li>
                  )}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ) : (
            <>
              {agent && roster?.budget?.exhausted ? (
                <div className="shrink-0 px-3 pb-3">
                  <BudgetHoldNotice
                    compact
                    agentId={agent.id}
                    budget={roster.budget}
                    heldCount={roster.waiting.length}
                    onGranted={refreshRoster}
                  />
                </div>
              ) : null}
              {agents === null || chats === null ? null : (
                <ThreadNavigation
                  key={agentId ?? ""}
                  threads={threads}
                  selected={selectedThread}
                  optimisticallyAwake={optimisticallyAwake}
                  onSelect={openThread}
                />
              )}
            </>
          )}
        </SidebarContent>

        <SidebarFooter className="border-t border-sidebar-border p-2">
          <DropdownMenu>
            <DropdownMenuTrigger
              className="flex h-11 w-full items-center gap-3 rounded-xl px-2 text-left outline-none transition-colors hover:bg-sidebar-accent focus-visible:ring-3 focus-visible:ring-ring/50 data-popup-open:bg-sidebar-accent"
              aria-label={`Account: ${userName}`}
            >
              <Avatar size="sm">
                {user?.picture ? <AvatarImage src={user.picture} alt="" /> : null}
                <AvatarFallback>{userName.slice(0, 1).toUpperCase()}</AvatarFallback>
              </Avatar>
              <span className="min-w-0 flex-1 truncate text-sm font-medium">{userName}</span>
              <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            </DropdownMenuTrigger>
            <DropdownMenuContent side="top" sideOffset={6}>
              <DropdownMenuGroup>
                <DropdownMenuLabel>Connections</DropdownMenuLabel>
                {user?.github ? (
                  <DropdownMenuItem onClick={disconnectGitHub}>
                    <GitBranchIcon />
                    Disconnect GitHub (@{user.github.login})
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem render={<a href="/api/connections/github/authorize" />}>
                    <GitBranchIcon />
                    Connect GitHub
                  </DropdownMenuItem>
                )}
                {user?.slack ? (
                  <DropdownMenuItem onClick={disconnectSlack}>
                    <MessageSquareIcon />
                    Disconnect Slack ({user.slack.team ?? user.slack.team_id})
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem render={<a href="/api/connections/slack/authorize" />}>
                    <MessageSquareIcon />
                    Connect Slack
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={async () => {
                  await apiFetch("/api/auth/logout", { method: "POST" });
                  window.location.reload();
                }}
              >
                <LogOutIcon />
                Sign out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </SidebarFooter>
      </Sidebar>
      <SidebarResize containerRef={layoutRef} paneRef={navigationRef} />
      <SidebarInset className="min-w-0">
        {conversation}
        {content}
      </SidebarInset>
    </SidebarProvider>
  );
}
