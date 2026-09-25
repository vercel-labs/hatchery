import { useLocation, useNavigate } from "@tanstack/react-router";
import { Activity, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArchiveIcon,
  Braces,
  CheckIcon,
  FolderGit2,
  FolderOpen,
  GitBranchIcon,
  LogOutIcon,
  MessageSquareIcon,
  Settings2,
  XIcon,
} from "lucide-react";

import {
  apiBase,
  apiFetch,
  type Agent,
  type AgentWarning,
  type Chat,
  type User,
} from "@/lib/api";
import type { AgentThreads } from "@/lib/api-types";
import { chatSidebarText, sidebarThreads } from "@/lib/chat-sidebar";
import { type AccentColor } from "@/lib/agent-colors";
import { number } from "@/lib/format";
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
import { Conversation } from "@/features/threads/conversation";
import { ThreadNavigation } from "@/features/threads/thread-navigation";
import { threadsWithObjectives } from "@/features/threads/thread-tree";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
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

type Selection =
  | { kind: "agent" | "api"; id: string }
  | { kind: "chat"; id: string }
  | { kind: "repository" }
  | null;

const AGENT_KEY = "hatchery:agent";

export function parseSelection(pathname: string): Selection {
  const agent = pathname.match(/^\/agents\/([^/]+)(\/api)?$/);
  if (agent)
    return { kind: agent[2] ? "api" : "agent", id: decodeURIComponent(agent[1]) };
  const chat = pathname.match(/^\/chats\/([^/]+)$/);
  if (chat) return { kind: "chat", id: decodeURIComponent(chat[1]) };
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
  const [settingsOpen, setSettingsOpen] = useState(false);
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
    selection?.kind === "agent" || selection?.kind === "api"
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
    setSettingsOpen(false);
    openNewChat(created.id);
  };

  const deleteAgent = async (target: Agent) => {
    if (!window.confirm(`Remove ${target.name}?`)) return;
    const res = await apiFetch(`/api/agents/${target.id}`, { method: "DELETE" });
    if (res.status === 409) {
      window.alert("Remove this agent's chats first.");
      return;
    }
    if (!res.ok) return;
    setAgents((current) => current?.filter((item) => item.id !== target.id) ?? null);
    void navigate({ to: "/" });
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
              Could not load agents and chats. Locally: run `uv run dev.py` in backend/ and reload.
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
            title={undefined}
          />
        )}
      </AllRosters>
    ) : null;
  } else if (selection?.kind === "agent" && agent) {
    content = (
      <AgentPage
        key={agent.id}
        agent={agent}
        roster={roster?.agent_id === agent.id ? roster : undefined}
        warning={selectedWarning}
        leading={leading}
        refreshRoster={refreshRoster}
        onChange={(updated) =>
          setAgents((current) => current?.map((item) => (item.id === updated.id ? updated : item)) ?? null)
        }
        onRemove={() => void deleteAgent(agent)}
      />
    );
  } else if (selection?.kind === "api" && agent) {
    content = (
      <AgentApiView key={agent.id} agentId={agent.id} agentName={agent.name} leading={leading} />
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
        <SidebarHeader className="gap-1 p-2">
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
            />
          ) : null}
          <nav aria-label="Views" className="flex flex-col gap-0.5">
            {agent ? (
              <>
                <Button
                  className="h-9 justify-start px-3 text-muted-foreground"
                  variant={selection?.kind === "agent" ? "secondary" : "ghost"}
                  aria-current={selection?.kind === "agent" ? "page" : undefined}
                  onClick={() => void navigate({ to: "/agents/$agentId", params: { agentId: agent.id } })}
                >
                  <FolderOpen />
                  Workspace
                </Button>
                <Button
                  className="h-9 justify-start px-3 text-muted-foreground"
                  variant={selection?.kind === "api" ? "secondary" : "ghost"}
                  aria-current={selection?.kind === "api" ? "page" : undefined}
                  onClick={() => void navigate({ to: "/agents/$agentId/api", params: { agentId: agent.id } })}
                >
                  <Braces />
                  API
                </Button>
              </>
            ) : null}
            <Button
              className="h-9 justify-start px-3 text-muted-foreground"
              variant={selection?.kind === "repository" ? "secondary" : "ghost"}
              aria-current={selection?.kind === "repository" ? "page" : undefined}
              onClick={() => void navigate({ to: "/repository" })}
            >
              <FolderGit2 />
              Repository
            </Button>
          </nav>
        </SidebarHeader>

        <SidebarContent className="gap-0">
          {archiveOpen ? (
            <SidebarGroup aria-label="Archived chats">
              <SidebarGroupLabel>Archive</SidebarGroupLabel>
              <SidebarGroupAction title="Close archive" aria-label="Close archive" onClick={() => setArchiveOpen(false)}>
                <XIcon />
              </SidebarGroupAction>
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
                  onSelect={(id) => (id ? openThread(id) : openNewChat())}
                />
              )}
            </>
          )}
        </SidebarContent>

        <SidebarFooter className="border-t border-sidebar-border">
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                aria-label={`Open archive, ${archivedChats.length} chats`}
                onClick={() => setArchiveOpen(!archiveOpen)}
                tooltip="Archive"
              >
                <ArchiveIcon />
                <span>Archive</span>
                {archivedChats.length > 0 && <span className="ml-auto tabular-nums">{archivedChats.length}</span>}
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
          <details
            className="group px-1"
            open={settingsOpen || (agents !== null && !agents.length)}
            onToggle={(event) => setSettingsOpen(event.currentTarget.open)}
          >
            <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-1 py-2 text-xs text-muted-foreground hover:text-foreground">
              <Settings2 className="size-3.5" />
              <span className="min-w-0 flex-1 truncate">
                {user?.name ?? user?.username ?? user?.email ?? "hatchery"}
              </span>
            </summary>
            <div className="flex flex-col gap-3 pt-2 pb-1">
              <form className="flex flex-col gap-2" onSubmit={createAgent}>
                <div className="flex gap-1">
                  <Input
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
                </div>
                <AgentColorPicker value={agentColor} onValueChange={setAgentColor} allowUnselected />
              </form>
              {roster?.budget ? (
                <p
                  className={
                    roster.budget.exhausted
                      ? "text-xs font-medium text-amber-700 dark:text-amber-300"
                      : "text-xs text-muted-foreground"
                  }
                >
                  {roster.budget.exhausted
                    ? "No tokens left today"
                    : `${number(roster.budget.remaining)} tokens left today`}
                </p>
              ) : null}
              <div className="flex flex-col gap-0.5">
                {user?.github ? (
                  <Button variant="ghost" size="sm" className="justify-start text-muted-foreground" onClick={disconnectGitHub}>
                    <GitBranchIcon />
                    Disconnect GitHub (@{user.github.login})
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="justify-start text-muted-foreground"
                    nativeButton={false}
                    render={<a href={`${apiBase()}/api/connections/github/authorize`} />}
                  >
                    <GitBranchIcon />
                    Connect GitHub
                  </Button>
                )}
                {user?.slack ? (
                  <Button variant="ghost" size="sm" className="justify-start text-muted-foreground" onClick={disconnectSlack}>
                    <MessageSquareIcon />
                    Disconnect Slack ({user.slack.team ?? user.slack.team_id})
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="justify-start text-muted-foreground"
                    nativeButton={false}
                    render={<a href={`${apiBase()}/api/connections/slack/authorize`} />}
                  >
                    <MessageSquareIcon />
                    Connect Slack
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  className="justify-start text-muted-foreground"
                  onClick={async () => {
                    await apiFetch("/api/auth/logout", { method: "POST" });
                    window.location.reload();
                  }}
                >
                  <LogOutIcon />
                  Sign out
                </Button>
              </div>
            </div>
          </details>
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
