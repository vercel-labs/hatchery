import { Link, useLocation, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArchiveIcon,
  BookMarkedIcon,
  CheckIcon,
  ChevronsUpDownIcon,
  FilterIcon,
  FolderGitIcon,
  GitBranchIcon,
  LinkIcon,
  PauseIcon,
  PlayIcon,
  TriangleAlertIcon,
  LogOutIcon,
  PencilIcon,
  PlusIcon,
  MessageSquareIcon,
  TerminalIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  apiBase,
  apiFetch,
  type Thread,
  type Job,
  type Resource,
  type Agent,
  type AgentWarning,
  type AppSettings,
  type User,
} from "@/lib/api";
import {
  chatAttentionFilterLabel,
  chatAttentionLabel,
  chatSidebarText,
  filterSidebarChats,
  selectSidebarAgent,
  type ChatSidebarFilters,
} from "@/lib/chat-sidebar";
import type { ChatUIMessage } from "@/lib/messages";
import {
  type AccentColor,
  normalizeAccentColor,
  resolveAgentColor,
} from "@/lib/agent-colors";
import { cn } from "@/lib/utils";
import { ChatView, NewChatView } from "@/components/chat";
import {
  createChatPersister,
  startsFreshDraft,
  type NewChatHandoff,
  type NewChatRequest,
} from "@/components/new-chat-state";
import { SandboxForm } from "@/components/sandbox-form";
import { AgentColorPicker } from "@/components/agent-color-picker";
import { TerminalPane, type SandboxWorkspace } from "@/components/terminal-pane";
import { AgentFilesView } from "@/features/agent-files";
import { AgentSchedulesView, AgentSwitcher } from "@/features/agents";
import { MemoryRepositoryOnboarding } from "@/features/onboarding";
import {
  ThreadNavigation,
  type ThreadNavigationItem,
} from "@/features/threads";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
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
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
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
import { Separator } from "@/components/ui/separator";
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
  SidebarTrigger,
} from "@/components/ui/sidebar";

type Selection =
  | { kind: "agent"; id: string; view: "overview" | "files" | "schedules" }
  | { kind: "thread"; id: string }
  | null;

type ThreadHierarchyNode = {
  id: string;
  kind: "thread" | "task" | "fx";
  parent_id: string | null;
  title: string;
  status: string | null;
  summary: string | null;
  children: ThreadHierarchyNode[];
};

type SidebarThread = ThreadNavigationItem & { rootThreadId: string };

function threadBotStatus(status: string | null) {
  return status === "running" || status === "pending" || status === "queued"
    ? "working" as const
    : status === "attention" || status === "errored" || status === "failed"
      ? "error" as const
      : status === "complete" || status === "done"
        ? "done" as const
        : "waiting" as const;
}

function sidebarThreads(root: ThreadHierarchyNode): SidebarThread[] {
  const found: SidebarThread[] = [];
  const visit = (node: ThreadHierarchyNode, parentId: string | null) => {
    const status = node.status ?? "waiting";
    const botStatus = threadBotStatus(node.status);
    found.push({
      id: node.id,
      parentId,
      rootThreadId: root.id,
      title: node.title,
      subtitle: node.summary ?? (node.kind === "task" ? status : undefined),
      bots:
        node.kind === "thread"
          ? node.children.map((child) => ({
              id: child.id,
              name: child.title,
              status: threadBotStatus(child.status),
            }))
          : [{ id: node.id, name: node.title, status: botStatus }],
    });
    node.children.forEach((child) => visit(child, node.id));
  };
  visit(root, null);
  return found;
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
  agentColor,
  selected,
  onArchiveChange,
}: {
  chat: Thread;
  agentColor: string | undefined;
  selected: boolean;
  onArchiveChange: (chat: Thread, archived: boolean) => void;
}) {
  const archived = chat.archived_at !== null;
  const attentionLabel = chatAttentionLabel(chat);
  const text = chatSidebarText(chat);
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        className="relative pl-3"
        isActive={selected}
        aria-current={selected ? "page" : undefined}
        render={<Link to="/threads/$threadId" params={{ threadId: chat.id }} />}
        tooltip={text.label}
        aria-label={text.label}
      >
        <span
          className="absolute inset-y-1 left-0 w-0.5 rounded-full"
          style={{ backgroundColor: resolveAgentColor(agentColor) }}
        />
        {attentionLabel && (
          <div
            className={cn(
              "size-2 shrink-0 rounded-full",
              chat.attention_reason === "result_available"
                ? "bg-status-green-700"
                : "bg-status-amber-700",
            )}
            title={attentionLabel}
            aria-label={attentionLabel}
          />
        )}
        <ChatOriginIcon trigger={chat.trigger} />
        <span className="truncate">
          {text.author ? <span className="font-medium">{text.author}</span> : null}
          {text.author && !text.fragment.startsWith("'") ? " " : null}
          {text.fragment}
        </span>
      </SidebarMenuButton>
      <SidebarMenuAction
        showOnHover
        aria-label={`${archived ? "Unarchive" : "Archive"} ${text.label}`}
        title={archived ? "Unarchive thread" : "Archive thread"}
        onClick={() => onArchiveChange(chat, !archived)}
      >
        <ArchiveIcon />
      </SidebarMenuAction>
    </SidebarMenuItem>
  );
}

export function AppShell() {
  const pathname = useLocation({ select: (location) => location.pathname });
  const navigate = useNavigate();
  const agentMatch = pathname.match(/^\/agents\/([^/]+)(?:\/(files|schedules))?$/);
  const chatMatch = pathname.match(/^\/threads\/([^/]+)$/);
  const selection: Selection = agentMatch
    ? {
        kind: "agent",
        id: decodeURIComponent(agentMatch[1]),
        view: (agentMatch[2] as "files" | "schedules" | undefined) ?? "overview",
      }
    : chatMatch
      ? { kind: "thread", id: decodeURIComponent(chatMatch[1]) }
      : null;
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [appSettings, setAppSettings] = useState<AppSettings | null>(null);
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [chats, setChats] = useState<Thread[] | null>(null);
  const [hierarchies, setHierarchies] = useState<Record<string, ThreadHierarchyNode>>({});
  const [selectedThreadNodeId, setSelectedThreadNodeId] = useState<string | null>(null);
  const [agentWarnings, setAgentWarnings] = useState<AgentWarning[]>([]);
  const [failed, setFailed] = useState(false);
  const [addingAgent, setAddingAgent] = useState(false);
  const [agentName, setAgentName] = useState("");
  const [agentColor, setAgentColor] = useState<AccentColor | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [chatFilters, setChatFilters] = useState<ChatSidebarFilters>({
    requiresAttention: false,
    agentId: null,
  });
  const [newChatGeneration, setNewChatGeneration] = useState(0);
  const newChatGenerationRef = useRef(0);
  const previousPath = useRef<string | null>(null);
  const draftPersister = useRef<
    ((agentId: string | null) => Promise<Thread>) | null
  >(null);
  const [chatHandoff, setChatHandoff] = useState<{
    chat: Thread;
    startup: NewChatHandoff;
  } | null>(null);

  useEffect(() => {
    if (startsFreshDraft(previousPath.current, pathname)) {
      draftPersister.current = null;
      setChatHandoff(null);
      newChatGenerationRef.current += 1;
      setNewChatGeneration(newChatGenerationRef.current);
    }
    previousPath.current = pathname;
  }, [pathname]);

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

  useEffect(() => {
    const load = async () => {
      const identity = await apiFetch("/api/auth/me");
      if (!identity.ok) throw new Error("backend unreachable");
      const me: { user: User | null } = await identity.json();
      if (!me.user) {
        setUser(null);
        return;
      }
      const [s, c, github, slack, warnings, configuration] = await Promise.all([
        apiFetch("/api/agents"),
        apiFetch("/api/threads"),
        apiFetch("/api/connections/github"),
        apiFetch("/api/connections/slack"),
        apiFetch("/api/agents/warnings"),
        apiFetch("/api/settings"),
      ]);
      if (
        !s.ok ||
        !c.ok ||
        !github.ok ||
        !slack.ok ||
        !warnings.ok ||
        !configuration.ok
      ) {
        throw new Error("backend unreachable");
      }
      const connection: { connection: User["github"] | null } = await github.json();
      const slackConnection: { connection: User["slack"] | null } = await slack.json();
      setUser({
        ...me.user,
        github: connection.connection ?? undefined,
        slack: slackConnection.connection ?? undefined,
      });
      setAppSettings(await configuration.json());
      setAgents(await s.json());
      setChats(await c.json());
      setAgentWarnings(await warnings.json());
    };
    load().catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    if (!chats) return;
    let current = true;
    const loadHierarchies = async () => {
      const active = chats.filter((chat) => chat.archived_at === null);
      const responses = await Promise.all(
        active.map(async (chat) => {
          const response = await apiFetch(`/api/threads/${chat.id}/hierarchy`);
          return response.ok
            ? ([chat.id, (await response.json()) as ThreadHierarchyNode] as const)
            : null;
        }),
      );
      if (current) {
        setHierarchies(
          Object.fromEntries(responses.filter((item) => item !== null)),
        );
      }
    };
    void loadHierarchies();
    const interval = window.setInterval(() => void loadHierarchies(), 5_000);
    return () => {
      current = false;
      window.clearInterval(interval);
    };
  }, [chats]);

  const colorOf = (agentId: string | null) =>
    agents?.find((s) => s.id === agentId)?.color;

  const selectedAgent =
    selection?.kind === "agent"
      ? (agents?.find((s) => s.id === selection.id) ?? null)
      : null;
  const routedChat =
    selection?.kind === "thread"
      ? (chats?.find((c) => c.id === selection.id) ?? null)
      : null;
  const activeChatHandoff =
    chatHandoff &&
    (selection === null ||
      (selection.kind === "thread" && selection.id === chatHandoff.chat.id))
      ? chatHandoff
      : null;
  const selectedChat = activeChatHandoff?.chat ?? routedChat;
  const activeAgent =
    selectedAgent ??
    agents?.find((agent) => agent.id === selectedChat?.agent_id) ??
    agents?.find((agent) => agent.id === chatFilters.agentId) ??
    agents?.[0] ??
    null;

  useEffect(() => {
    if (
      !chatHandoff ||
      pathname !== `/threads/${encodeURIComponent(chatHandoff.chat.id)}`
    ) {
      return;
    }
    const timeout = window.setTimeout(() => {
      setChatHandoff((current) =>
        current?.chat.id === chatHandoff.chat.id ? null : current,
      );
    });
    return () => window.clearTimeout(timeout);
  }, [chatHandoff, pathname]);

  const selectedWarning = agentWarnings.find(
    (warning) =>
      warning.agent_id === (selectedAgent?.id ?? selectedChat?.agent_id),
  );

  const filteredChats = chats ? filterSidebarChats(chats, chatFilters) : null;
  const threadNavigationItems = (filteredChats ?? []).flatMap((chat) =>
    hierarchies[chat.id]
      ? sidebarThreads(hierarchies[chat.id])
      : [{ id: chat.id, parentId: null, rootThreadId: chat.id, title: chatSidebarText(chat).label }],
  );
  const selectedHierarchyItem = threadNavigationItems.find(
    (item) =>
      item.id === selectedThreadNodeId && item.rootThreadId === selectedChat?.id,
  );
  const activeThreadNodeId = selectedHierarchyItem?.id ?? selectedChat?.id;
  const filteredAgent = agents?.find((agent) => agent.id === chatFilters.agentId);
  const activeFilterCount =
    Number(chatFilters.requiresAttention) + Number(chatFilters.agentId !== null);
  const archivedChats = chats
    ?.filter((chat) => chat.archived_at !== null)
    .sort((a, b) => (b.archived_at ?? "").localeCompare(a.archived_at ?? "")) ?? [];

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
    if (!res.ok) return;
    const agent: Agent = await res.json();
    setAgents((current) => [...(current ?? []), agent]);
    await navigate({ to: "/agents/$agentId", params: { agentId: agent.id } });
    setChatFilters((current) => selectSidebarAgent(current, agent.id));
    setAgentName("");
    setAgentColor(null);
    setAddingAgent(false);
  };

  const deleteAgent = async (agent: Agent) => {
    if (!window.confirm(`Remove ${agent.name}?`)) return;
    const res = await apiFetch(`/api/agents/${agent.id}`, { method: "DELETE" });
    if (res.status === 409) {
      window.alert("Remove this agent's threads first.");
      return;
    }
    if (!res.ok) return;
    setAgents((current) => current?.filter((item) => item.id !== agent.id) ?? null);
    if (selection?.kind === "agent" && selection.id === agent.id) {
      void navigate({ to: "/" });
    }
    setChatFilters((current) =>
      current.agentId === agent.id ? selectSidebarAgent(current, null) : current,
    );
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
        const response = await apiFetch("/api/threads");
        const found: Thread[] | null = response.ok ? await response.json() : null;
        if (found) setChats(found);
      } catch {}
    } while (refreshChatsAgain.current);
    refreshingChats.current = false;
  }, []);

  const persistDraftChat = useCallback((agentId: string | null) => {
    if (!draftPersister.current) {
      draftPersister.current = createChatPersister(
        async (request: NewChatRequest) => {
          const response = await apiFetch("/api/threads", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(request),
          });
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.detail ?? "Could not create thread");
          }
          const chat: Thread = await response.json();
          setChats((current) => [
            chat,
            ...(current ?? []).filter((item) => item.id !== chat.id),
          ]);
          return chat;
        },
      );
    }
    return draftPersister.current(agentId);
  }, []);

  const openNewChat = () => {
    draftPersister.current = null;
    setChatHandoff(null);
    newChatGenerationRef.current += 1;
    setNewChatGeneration(newChatGenerationRef.current);
    if (pathname !== "/") void navigate({ to: "/" });
  };

  const openPersistedChat = useCallback(
    (chatId: string) => {
      void navigate({
        to: "/threads/$threadId",
        params: { threadId: chatId },
        replace: true,
      });
    },
    [navigate],
  );

  const handoffPersistedChat = useCallback(
    (chat: Thread, startup: NewChatHandoff) => {
      setChatHandoff({ chat, startup });
      void navigate({
        to: "/threads/$threadId",
        params: { threadId: chat.id },
        replace: true,
      });
    },
    [navigate],
  );

  const setChatArchived = async (chat: Thread, archived: boolean) => {
    const res = await apiFetch(`/api/threads/${chat.id}/archive`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archived }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      window.alert(body.detail ?? `Could not ${archived ? "archive" : "unarchive"} chat.`);
      return;
    }
    const updated: Thread = await res.json();
    setChats((current) =>
      current?.map((item) => (item.id === updated.id ? updated : item)) ?? null,
    );
  };

  const updateChat = useCallback((updated: Thread) => {
    setChats((current) =>
      current?.map((item) => (item.id === updated.id ? updated : item)) ?? null,
    );
  }, []);

  const assignChatAgent = async (chat: Thread, agentId: string) => {
    const res = await apiFetch(`/api/threads/${chat.id}/agent`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId }),
    });
    if (!res.ok) return;
    const updated: Thread = await res.json();
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

  if (!appSettings) return <div className="h-svh" />;

  if (!appSettings.configured) {
    return (
      <MemoryRepositoryOnboarding
        github={user.github}
        onComplete={setAppSettings}
      />
    );
  }

  return (
    <SidebarProvider className="h-svh overflow-hidden">
      <Sidebar>
        <SidebarHeader className="h-14 border-b border-sidebar-border p-2">
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <SidebarMenuButton size="lg" className="h-full py-0">
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

        <SidebarContent>
          {!archiveOpen && activeAgent ? (
            <div className="px-3 pt-3">
              <AgentSwitcher
                agents={(agents ?? []).map((agent) => ({
                  id: agent.id,
                  name: agent.name,
                  description: agent.slug,
                  status: "online",
                }))}
                value={activeAgent.id}
                onValueChange={(agentId) =>
                  void navigate({ to: "/agents/$agentId", params: { agentId } })
                }
              />
              <div className="mt-2 grid grid-cols-3 gap-1">
                <Button
                  size="xs"
                  variant={selection?.kind === "agent" && selection.view === "overview" ? "secondary" : "ghost"}
                  render={<Link to="/agents/$agentId" params={{ agentId: activeAgent.id }} />}
                >
                  Overview
                </Button>
                <Button
                  size="xs"
                  variant={selection?.kind === "agent" && selection.view === "files" ? "secondary" : "ghost"}
                  render={<Link to="/agents/$agentId/files" params={{ agentId: activeAgent.id }} />}
                >
                  Files
                </Button>
                <Button
                  size="xs"
                  variant={selection?.kind === "agent" && selection.view === "schedules" ? "secondary" : "ghost"}
                  render={<Link to="/agents/$agentId/schedules" params={{ agentId: activeAgent.id }} />}
                >
                  Schedules
                </Button>
              </div>
            </div>
          ) : null}
          {archiveOpen ? (
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
                        agentColor={colorOf(chat.agent_id)}
                        selected={selectedChat?.id === chat.id}
                        onArchiveChange={(item, archived) =>
                          void setChatArchived(item, archived)
                        }
                      />
                    ))
                  ) : (
                    <li className="px-2 py-4 text-sm text-muted-foreground">
                      No archived threads
                    </li>
                  )}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ) : (
            <>
              <SidebarGroup>
                <SidebarGroupLabel>Agents</SidebarGroupLabel>
                <SidebarGroupAction
                  title="New agent"
                  aria-label="New agent"
                  onClick={() => setAddingAgent(true)}
                >
                  <PlusIcon />
                </SidebarGroupAction>
                <SidebarGroupContent>
                  {addingAgent && (
                    <form className="flex flex-col gap-2 px-2 pb-2" onSubmit={createAgent}>
                      <div className="flex gap-1">
                        <Input
                          autoFocus
                          value={agentName}
                          onChange={(event) => setAgentName(event.target.value)}
                          placeholder="Agent name"
                          aria-label="Agent name"
                          className="h-7"
                        />
                        <Button type="submit" size="icon-xs" disabled={!agentName.trim()}>
                          <CheckIcon />
                          <span className="sr-only">Add agent</span>
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          onClick={() => {
                            setAddingAgent(false);
                            setAgentName("");
                            setAgentColor(null);
                          }}
                        >
                          <XIcon />
                          <span className="sr-only">Cancel</span>
                        </Button>
                      </div>
                      <AgentColorPicker
                        value={agentColor}
                        onValueChange={setAgentColor}
                        allowUnselected
                      />
                    </form>
                  )}
                  <SidebarMenu>
                    {agents === null
                      ? Array.from({ length: failed ? 0 : 2 }).map((_, i) => (
                          <SidebarMenuItem key={i}>
                            <SidebarMenuSkeleton />
                          </SidebarMenuItem>
                        ))
                      : agents.map((agent) => (
                          <SidebarMenuItem key={agent.id}>
                            <SidebarMenuButton
                              className="relative pl-3"
                              isActive={selectedAgent?.id === agent.id}
                              onClick={() =>
                                setChatFilters((current) =>
                                  selectSidebarAgent(current, agent.id),
                                )
                              }
                              render={
                                <Link
                                  to="/agents/$agentId"
                                  params={{ agentId: agent.id }}
                                />
                              }
                              tooltip={agent.name}
                            >
                              <span
                                className="absolute inset-y-1 left-0 w-1 rounded-full"
                                style={{ backgroundColor: resolveAgentColor(agent.color) }}
                              />
                              <span className="truncate">{agent.name}</span>
                            </SidebarMenuButton>
                            <SidebarMenuAction
                              showOnHover
                              aria-label={`Remove ${agent.name}`}
                              title={`Remove ${agent.name}`}
                              onClick={() => deleteAgent(agent)}
                            >
                              <Trash2Icon />
                            </SidebarMenuAction>
                          </SidebarMenuItem>
                        ))}
                  </SidebarMenu>
                </SidebarGroupContent>
              </SidebarGroup>

              <SidebarGroup>
                <SidebarGroupLabel>Threads</SidebarGroupLabel>
                <DropdownMenu>
                  <DropdownMenuTrigger
                    render={
                      <SidebarGroupAction
                        className="right-9 [&>svg]:size-3"
                        title="Filter threads"
                        aria-label={`Filter threads${activeFilterCount ? `, ${activeFilterCount} active` : ""}`}
                      >
                        <FilterIcon />
                      </SidebarGroupAction>
                    }
                  />
                  <DropdownMenuContent side="right" align="start" className="w-56">
                    <DropdownMenuGroup>
                      <DropdownMenuLabel>Filter threads</DropdownMenuLabel>
                      <DropdownMenuCheckboxItem
                        checked={chatFilters.requiresAttention}
                        onCheckedChange={(checked) =>
                          setChatFilters((current) => ({
                            ...current,
                            requiresAttention: checked,
                          }))
                        }
                      >
                        {chatAttentionFilterLabel}
                      </DropdownMenuCheckboxItem>
                    </DropdownMenuGroup>
                    <DropdownMenuSeparator />
                    <DropdownMenuGroup>
                      <DropdownMenuLabel>Agent</DropdownMenuLabel>
                      <DropdownMenuRadioGroup
                        value={chatFilters.agentId ?? "__all__"}
                        onValueChange={(value) =>
                          setChatFilters((current) =>
                            selectSidebarAgent(
                              current,
                              value === "__all__" ? null : value,
                            ),
                          )
                        }
                      >
                        <DropdownMenuRadioItem value="__all__">
                          All agents
                        </DropdownMenuRadioItem>
                        {agents?.map((agent) => (
                          <DropdownMenuRadioItem key={agent.id} value={agent.id}>
                            {agent.name}
                          </DropdownMenuRadioItem>
                        ))}
                      </DropdownMenuRadioGroup>
                    </DropdownMenuGroup>
                  </DropdownMenuContent>
                </DropdownMenu>
                <SidebarGroupAction
                  title="New thread"
                  aria-label="New thread"
                  onClick={openNewChat}
                >
                  <PlusIcon />
                </SidebarGroupAction>
                <SidebarGroupContent>
                  {activeFilterCount > 0 && (
                    <div className="my-3 flex flex-wrap gap-1">
                      {chatFilters.requiresAttention && (
                        <Badge
                          variant="secondary"
                          render={
                            <button
                              type="button"
                              aria-label={`Remove ${chatAttentionFilterLabel} filter`}
                              onClick={() =>
                                setChatFilters((current) => ({
                                  ...current,
                                  requiresAttention: false,
                                }))
                              }
                            />
                          }
                        >
                          {chatAttentionFilterLabel}
                          <XIcon data-icon="inline-end" />
                        </Badge>
                      )}
                      {chatFilters.agentId && (
                        <Badge
                          variant="secondary"
                          render={
                            <button
                              type="button"
                              aria-label={`Remove ${filteredAgent?.name ?? "agent"} filter`}
                              onClick={() =>
                                setChatFilters((current) =>
                                  selectSidebarAgent(current, null),
                                )
                              }
                            />
                          }
                        >
                          <span
                            aria-hidden="true"
                            className="size-1.5 shrink-0 rounded-full"
                            style={{
                              backgroundColor: resolveAgentColor(filteredAgent?.color),
                            }}
                          />
                          {filteredAgent?.name ?? "Unknown agent"}
                          <XIcon data-icon="inline-end" />
                        </Badge>
                      )}
                    </div>
                  )}
                  {filteredChats === null ? (
                    <SidebarMenu>
                      {Array.from({ length: failed ? 0 : 4 }).map((_, i) => (
                        <SidebarMenuItem key={i}>
                          <SidebarMenuSkeleton />
                        </SidebarMenuItem>
                      ))}
                    </SidebarMenu>
                  ) : (
                    <ThreadNavigation
                      className="max-h-[55svh]"
                      threads={threadNavigationItems}
                      activeThreadId={activeThreadNodeId}
                      onThreadSelect={(item) => {
                        const selected = threadNavigationItems.find(
                          (candidate) => candidate.id === item.id,
                        );
                        if (selected) {
                          setSelectedThreadNodeId(selected.id);
                          void navigate({
                            to: "/threads/$threadId",
                            params: { threadId: selected.rootThreadId },
                          });
                        }
                      }}
                      emptyTitle="No threads"
                      emptyDescription={
                        activeFilterCount > 0
                          ? "No threads match these filters."
                          : "Start a conversation to create a thread."
                      }
                    />
                  )}
                </SidebarGroupContent>
              </SidebarGroup>
            </>
          )}
        </SidebarContent>
        {!archiveOpen && (
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

      <SidebarInset>
        <header className="flex h-14 items-center gap-2 border-b px-4">
          <SidebarTrigger />
          <Separator orientation="vertical" className="h-4" />
          <span className="min-w-0 flex-1 truncate text-sm font-medium">
            {selectedAgent?.name ??
              (selectedChat
                ? chatSidebarText(selectedChat).label
                : selection
                  ? "hatchery"
                  : "New thread")}
          </span>
        </header>
        {selectedChat && !failed ? (
          <LiveChat
            key={selectedChat.id}
            chat={selectedChat}
            agents={agents ?? []}
            preferredTaskId={
              selectedHierarchyItem &&
              selectedHierarchyItem.id !== selectedChat.id &&
              !selectedHierarchyItem.id.includes(":")
                ? selectedHierarchyItem.id
                : undefined
            }
            warning={selectedWarning?.warning}
            handoff={
              activeChatHandoff?.chat.id === selectedChat.id
                ? activeChatHandoff.startup
                : undefined
            }
            onChatChanged={refreshChats}
            onChatUpdated={updateChat}
            onAgentChange={(agentId) =>
              assignChatAgent(selectedChat, agentId)
            }
            onUnarchive={() => void setChatArchived(selectedChat, false)}
            onCreateAgent={() => setAddingAgent(true)}
            onAgentAssigned={(agentId) =>
              setChats((current) => {
                if (
                  current?.find((chat) => chat.id === selectedChat.id)
                    ?.agent_id === agentId
                ) {
                  return current;
                }
                return (
                  current?.map((chat) =>
                    chat.id === selectedChat.id
                      ? { ...chat, agent_id: agentId }
                      : chat,
                  ) ?? null
                );
              })
            }
          />
        ) : (
          <div className="flex flex-1 overflow-y-auto p-6 md:p-10">
            {failed ? (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Backend unreachable</EmptyTitle>
                  <EmptyDescription>
                    Could not load agents and threads. Locally: run `uv run
                    dev.py` in backend/ and reload.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : selectedAgent ? (
              <AgentPane
                key={selectedAgent.id}
                agent={selectedAgent}
                view={selection?.kind === "agent" ? selection.view : "overview"}
                warning={selectedWarning?.warning}
                onChange={(updated) =>
                  setAgents((current) =>
                    current?.map((agent) =>
                      agent.id === updated.id ? updated : agent,
                    ) ?? null,
                  )
                }
              />
            ) : selection && agents !== null && chats !== null ? (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle>Not found</EmptyTitle>
                  <EmptyDescription>
                    This {selection.kind} does not exist or you cannot access it.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : (
              <NewChatView
                key={newChatGeneration}
                agents={agents ?? []}
                onPersist={persistDraftChat}
                onHandoff={handoffPersistedChat}
                onOpenChat={openPersistedChat}
                onCreateAgent={() => setAddingAgent(true)}
                isCurrent={() =>
                  window.location.pathname === "/" &&
                  newChatGenerationRef.current === newChatGeneration
                }
              />
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

function AgentPane({
  agent,
  view,
  warning,
  onChange,
}: {
  agent: Agent;
  view: "overview" | "files" | "schedules";
  warning?: string;
  onChange: (agent: Agent) => void;
}) {
  const [editingDocument, setEditingDocument] = useState(false);
  const [documentName, setDocumentName] = useState(agent.name);
  const [documentAbout, setDocumentAbout] = useState(agent.about);
  const [documentColor, setDocumentColor] = useState<AccentColor | null>(() =>
    normalizeAccentColor(agent.color),
  );
  const [savingDocument, setSavingDocument] = useState(false);
  const [documentError, setDocumentError] = useState("");
  const [editingResources, setEditingResources] = useState(false);
  const [repos, setRepos] = useState(agent.repos);
  const [links, setLinks] = useState(agent.resources);
  const [kind, setKind] = useState<"repo" | "link">("repo");
  const [resourceTitle, setResourceTitle] = useState("");
  const [url, setUrl] = useState("");
  const [savingResources, setSavingResources] = useState(false);
  const [resourceError, setResourceError] = useState("");
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [jobsLoadError, setJobsLoadError] = useState(false);
  const [jobEditorOpen, setJobEditorOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<Job | null>(null);
  const [jobSchedule, setJobSchedule] = useState("");
  const [jobPrompt, setJobPrompt] = useState("");
  const [jobErrors, setJobErrors] = useState<{
    schedule?: string;
    prompt?: string;
    form?: string;
  }>({});
  const [jobBusy, setJobBusy] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    apiFetch(`/api/agents/${agent.id}/jobs`)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return (await response.json()) as Job[];
      })
      .then((found) => {
        if (current) setJobs(found);
      })
      .catch(() => {
        if (current) setJobsLoadError(true);
      });
    return () => {
      current = false;
    };
  }, [agent.id]);

  if (view === "files") {
    return <AgentFilesView agentId={agent.id} />;
  }
  if (view === "schedules") {
    return <AgentSchedulesView agentId={agent.id} />;
  }

  const resources = [
    ...agent.repos.map((repo) => ({
      title: repo,
      url: `https://github.com/${repo}`,
      kind: "repo",
    })),
    ...agent.resources,
  ];

  const startEditingDocument = () => {
    setDocumentName(agent.name);
    setDocumentAbout(agent.about);
    setDocumentColor(normalizeAccentColor(agent.color));
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
      const response = await apiFetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: documentName,
          about: documentAbout,
          ...(documentColor ? { color: documentColor } : {}),
        }),
      });
      if (!response.ok) throw new Error();
      onChange(await response.json());
      setEditingDocument(false);
    } catch {
      setDocumentError("Could not save agent.");
    } finally {
      setSavingDocument(false);
    }
  };

  const startEditingResources = () => {
    setRepos(agent.repos);
    setLinks(agent.resources);
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
      const response = await apiFetch(`/api/agents/${agent.id}/resources`, {
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

  const closeJobEditor = () => {
    setJobEditorOpen(false);
    setEditingJob(null);
    setJobSchedule("");
    setJobPrompt("");
    setJobErrors({});
  };

  const openJob = (job: Job | null) => {
    setJobEditorOpen(true);
    setEditingJob(job);
    setJobSchedule(job?.schedule ?? "0 9 * * 1-5");
    setJobPrompt(job?.prompt ?? "");
    setJobErrors({});
  };

  const saveJob = async (event: React.FormEvent) => {
    event.preventDefault();
    setJobBusy("save");
    setJobErrors({});
    try {
      const path = editingJob
        ? `/api/jobs/${editingJob.id}`
        : `/api/agents/${agent.id}/jobs`;
      const response = await apiFetch(path, {
        method: editingJob ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ schedule: jobSchedule, prompt: jobPrompt }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const details = Array.isArray(body.detail) ? body.detail : [];
        setJobErrors({
          schedule: details.find((item: { loc?: string[] }) => item.loc?.at(-1) === "schedule")?.msg,
          prompt: details.find((item: { loc?: string[] }) => item.loc?.at(-1) === "prompt")?.msg,
          form: details.length ? undefined : body.detail ?? "Could not save job.",
        });
        return;
      }
      const saved: Job = await response.json();
      setJobs((current) =>
        editingJob
          ? (current ?? []).map((job) => (job.id === saved.id ? saved : job))
          : [...(current ?? []), saved],
      );
      closeJobEditor();
    } catch {
      setJobErrors({ form: "Could not save job." });
    } finally {
      setJobBusy(null);
    }
  };

  const setJobPaused = async (job: Job) => {
    setJobBusy(job.id);
    setJobErrors({});
    try {
      const response = await apiFetch(`/api/jobs/${job.id}/pause`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: !job.paused }),
      });
      if (!response.ok) throw new Error();
      const updated: Job = await response.json();
      setJobs((current) =>
        current?.map((item) => (item.id === updated.id ? updated : item)) ?? null,
      );
    } catch {
      setJobErrors({ form: `Could not ${job.paused ? "resume" : "pause"} job.` });
    } finally {
      setJobBusy(null);
    }
  };

  const deleteJob = async (job: Job) => {
    if (!window.confirm("Delete this scheduled job?")) return;
    setJobBusy(job.id);
    setJobErrors({});
    try {
      const response = await apiFetch(`/api/jobs/${job.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error();
      setJobs((current) => current?.filter((item) => item.id !== job.id) ?? null);
    } catch {
      setJobErrors({ form: "Could not delete job." });
    } finally {
      setJobBusy(null);
    }
  };

  return (
    <div className="mx-auto grid w-full max-w-6xl gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <section className="mx-auto flex w-full max-w-2xl min-w-0 flex-col gap-6">
        {warning && <RepositoryWarning warning={warning} />}
        {editingDocument ? (
          <FieldGroup>
            <Field data-invalid={Boolean(documentError)}>
              <FieldLabel htmlFor={`agent-name-${agent.id}`}>Title</FieldLabel>
              <Input
                id={`agent-name-${agent.id}`}
                value={documentName}
                onChange={(event) => setDocumentName(event.target.value)}
                aria-invalid={Boolean(documentError)}
              />
            </Field>
            <Field>
              <FieldLabel>Accent color</FieldLabel>
              <AgentColorPicker
                value={documentColor}
                onValueChange={setDocumentColor}
                label={`Accent color for ${agent.name}`}
              />
              {!normalizeAccentColor(agent.color) && (
                <FieldDescription className="flex items-center gap-2">
                  <span
                    className="size-3 shrink-0 rounded-full"
                    style={{ backgroundColor: resolveAgentColor(agent.color) }}
                  />
                  The current custom color is kept unless you choose a new accent.
                </FieldDescription>
              )}
            </Field>
            <Field data-invalid={Boolean(documentError)}>
              <FieldLabel htmlFor={`agent-about-${agent.id}`}>Markdown</FieldLabel>
              <Textarea
                id={`agent-about-${agent.id}`}
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
              <h1 className="text-3xl font-semibold tracking-tight">{agent.name}</h1>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Edit agent"
                onClick={startEditingDocument}
              >
                <PencilIcon />
              </Button>
            </div>
            <article className="typeset typeset-docs min-w-0">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{agent.about}</ReactMarkdown>
            </article>
          </>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" render={<Link to="/agents/$agentId/files" params={{ agentId: agent.id }} />}>
            Agent files
          </Button>
          <Button variant="outline" render={<Link to="/agents/$agentId/schedules" params={{ agentId: agent.id }} />}>
            Schedules
          </Button>
        </div>
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
                  <FieldLabel htmlFor={`resource-kind-${agent.id}`}>Add resource</FieldLabel>
                  <select
                    id={`resource-kind-${agent.id}`}
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
                    <FieldLabel htmlFor={`resource-title-${agent.id}`}>Title</FieldLabel>
                    <Input
                      id={`resource-title-${agent.id}`}
                      value={resourceTitle}
                      onChange={(event) => setResourceTitle(event.target.value)}
                      placeholder="Documentation"
                    />
                  </Field>
                )}
                <Field data-invalid={Boolean(resourceError)}>
                  <FieldLabel htmlFor={`resource-url-${agent.id}`}>
                    {kind === "repo" ? "Repository" : "URL"}
                  </FieldLabel>
                  <Input
                    id={`resource-url-${agent.id}`}
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
        <Separator className="my-3" />
        <div className="flex h-7 items-center justify-between px-1">
          <span className="text-xs font-medium text-muted-foreground">Jobs</span>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Add job"
            onClick={() => openJob(null)}
          >
            <PlusIcon />
          </Button>
        </div>
        {jobEditorOpen && (
          <form onSubmit={saveJob}>
            <Card size="sm">
              <CardHeader>
                <CardTitle>{editingJob ? "Edit job" : "New job"}</CardTitle>
                <CardDescription>Schedules use UTC.</CardDescription>
              </CardHeader>
              <CardContent>
                <FieldGroup>
                  <Field data-invalid={Boolean(jobErrors.schedule)}>
                    <FieldLabel htmlFor={`job-schedule-${agent.id}`}>Schedule</FieldLabel>
                    <Input
                      id={`job-schedule-${agent.id}`}
                      value={jobSchedule}
                      onChange={(event) => setJobSchedule(event.target.value)}
                      placeholder="0 9 * * 1-5"
                      className="font-mono"
                      aria-invalid={Boolean(jobErrors.schedule)}
                      aria-describedby={
                        jobErrors.schedule
                          ? `job-schedule-help-${agent.id} job-schedule-error-${agent.id}`
                          : `job-schedule-help-${agent.id}`
                      }
                    />
                    <FieldDescription id={`job-schedule-help-${agent.id}`}>
                      Five-field cron expression in UTC.
                    </FieldDescription>
                    <FieldError id={`job-schedule-error-${agent.id}`}>
                      {jobErrors.schedule}
                    </FieldError>
                  </Field>
                  <Field data-invalid={Boolean(jobErrors.prompt)}>
                    <FieldLabel htmlFor={`job-prompt-${agent.id}`}>Prompt</FieldLabel>
                    <Textarea
                      id={`job-prompt-${agent.id}`}
                      value={jobPrompt}
                      onChange={(event) => setJobPrompt(event.target.value)}
                      aria-invalid={Boolean(jobErrors.prompt)}
                      aria-describedby={
                        jobErrors.prompt ? `job-prompt-error-${agent.id}` : undefined
                      }
                    />
                    <FieldError id={`job-prompt-error-${agent.id}`}>
                      {jobErrors.prompt}
                    </FieldError>
                  </Field>
                  <FieldError>{jobErrors.form}</FieldError>
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="ghost"
                      disabled={jobBusy === "save"}
                      onClick={closeJobEditor}
                    >
                      Cancel
                    </Button>
                    <Button type="submit" disabled={jobBusy === "save"}>
                      {jobBusy === "save" ? "Saving" : "Save"}
                    </Button>
                  </div>
                </FieldGroup>
              </CardContent>
            </Card>
          </form>
        )}
        {!jobEditorOpen && jobErrors.form && <FieldError>{jobErrors.form}</FieldError>}
        {jobs === null && !jobsLoadError && (
          <span className="px-1 text-sm text-muted-foreground">Loading jobs…</span>
        )}
        {jobsLoadError && (
          <span className="px-1 text-sm text-destructive">Could not load jobs.</span>
        )}
        {jobs?.map((job) => (
          <Card key={job.id} size="sm">
            <CardHeader>
              <CardTitle className="truncate">{job.prompt}</CardTitle>
              <CardDescription>
                <span className="font-mono">
                  {job.schedule} UTC{job.paused ? " · paused" : ""}
                </span>
                {job.author_display_name ? ` · by ${job.author_display_name}` : ""}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex justify-end gap-1">
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={job.paused ? "Resume job" : "Pause job"}
                disabled={jobBusy === job.id}
                onClick={() => setJobPaused(job)}
              >
                {job.paused ? <PlayIcon /> : <PauseIcon />}
              </Button>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Edit job"
                disabled={jobBusy === job.id}
                onClick={() => openJob(job)}
              >
                <PencilIcon />
              </Button>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Delete job"
                disabled={jobBusy === job.id}
                onClick={() => deleteJob(job)}
              >
                <Trash2Icon />
              </Button>
            </CardContent>
          </Card>
        ))}
        {jobs?.length === 0 && !jobEditorOpen && (
          <span className="px-1 text-sm text-muted-foreground">No jobs yet.</span>
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
  agents,
  preferredTaskId,
  warning,
  handoff,
  onChatChanged,
  onChatUpdated,
  onAgentChange,
  onUnarchive,
  onCreateAgent,
  onAgentAssigned,
}: {
  chat: Thread;
  agents: Agent[];
  preferredTaskId?: string;
  warning?: string;
  handoff?: NewChatHandoff;
  onChatChanged: () => void;
  onChatUpdated: (chat: Thread) => void;
  onAgentChange: (agentId: string) => void | Promise<void>;
  onUnarchive: () => void;
  onCreateAgent: () => void;
  onAgentAssigned: (agentId: string) => void;
}) {
  const [startup] = useState(handoff);
  const [initialMessages, setInitialMessages] = useState<
    ChatUIMessage[] | null
  >(startup?.loadMessages === false ? [] : null);
  const [sandboxes, setSandboxes] = useState<SandboxWorkspace[]>([]);
  const [messageRevision, setMessageRevision] = useState(0);
  const [streamGeneration, setStreamGeneration] = useState(0);
  const [showTerminal, setShowTerminal] = useState(false);
  const [showSandboxForm, setShowSandboxForm] = useState(false);
  const [preferredSandboxId, setPreferredSandboxId] = useState<string>();
  const loadingSandboxes = useRef(false);
  const reloadSandboxes = useRef(false);

  const loadSandboxes = useCallback(async () => {
    if (loadingSandboxes.current) {
      reloadSandboxes.current = true;
      return;
    }
    loadingSandboxes.current = true;
    do {
      reloadSandboxes.current = false;
      try {
        const response = await apiFetch(`/api/threads/${chat.id}/sandboxes`);
        const found: SandboxWorkspace[] = response.ok ? await response.json() : [];
        setSandboxes(found);
        if (found.length) setShowTerminal(true);
      } catch {
        setSandboxes([]);
      }
    } while (reloadSandboxes.current);
    loadingSandboxes.current = false;
  }, [chat.id]);

  useEffect(() => {
    if (startup?.loadMessages !== false) {
      apiFetch(`/api/threads/${chat.id}/messages`)
        .then((res) => (res.ok ? res.json() : []))
        .then(setInitialMessages)
        .catch(() => setInitialMessages([]));
    }
    const frame = requestAnimationFrame(loadSandboxes);
    return () => cancelAnimationFrame(frame);
  }, [chat.id, loadSandboxes, startup]);

  useEffect(() => {
    const source = new EventSource(
      `${apiBase()}/api/threads/${chat.id}/events`,
      { withCredentials: true },
    );
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as {
        type?: string;
        generation?: number;
        state?: string;
      };
      if (event.type === "chat.changed") {
        onChatChanged();
      }
      if (
        event.type === "sandbox.changed" ||
        (event.type === "task.changed" &&
          ["pending", "attention", "complete", "errored", "cancelled"].includes(
            event.state ?? "",
          ))
      ) {
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
            part.type === "data-agent-assignment" &&
            part.data.state === "assigned",
        );
      if (
        assignment?.type === "data-agent-assignment" &&
        assignment.data.agent_id
      ) {
        onAgentAssigned(assignment.data.agent_id);
      }
    },
    [onAgentAssigned],
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
            agentId={chat.agent_id}
            agents={agents}
            messageRevision={messageRevision}
            streamGeneration={streamGeneration}
            traceId={chat.telemetry_span?.trace_id ?? null}
            archived={chat.archived_at !== null}
            attentionReason={chat.attention_reason}
            handoff={startup}
            onMessagesChange={onMessagesChange}
            onSeen={onChatUpdated}
            onAgentChange={onAgentChange}
            onUnarchive={onUnarchive}
            onCreateSandbox={() => setShowSandboxForm(true)}
            onCreateAgent={onCreateAgent}
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
            key={`${chat.id}:${preferredSandboxId ?? ""}:${preferredTaskId ?? ""}`}
            chatId={chat.id}
            sandboxes={sandboxes}
            preferredSandboxId={preferredSandboxId}
            preferredTaskId={preferredTaskId}
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
