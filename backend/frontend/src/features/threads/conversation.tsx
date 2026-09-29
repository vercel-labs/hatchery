import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import {
  Activity,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";
import { Check, TriangleAlertIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ContextTabs, type ContextTab } from "@/components/context-tabs";
import { PromptForm } from "@/components/prompt-form";
import { TerminalPane, type SandboxWorkspace } from "@/components/terminal-pane";
import {
  newChatHandoff,
  streamAttachmentAction,
} from "@/components/new-chat-state";
import { submissionLabel } from "@/components/chat-status";
import { RepositoryPanelSkeleton } from "@/features/repository/repository-skeletons";
import { EmptyChanges } from "@/features/repository/empty-changes";
import { BudgetHoldNotice } from "@/features/agents/budget-hold-notice";
import { useApi } from "@/hooks/use-api";
import { apiFetch, type Agent, type Chat } from "@/lib/api";
import type {
  AgentThreads,
  Message,
  PendingPrompt,
  Repository,
  Thread,
} from "@/lib/api-types";
import { chatSidebarText } from "@/lib/chat-sidebar";
import { number } from "@/lib/format";
import type { ChatUIMessage } from "@/lib/messages";
import { liveMessages } from "./live-transcript";
import { QueuedPrompts } from "./queued-prompts";
import { Transcript } from "./transcript";
import { useThread } from "./use-thread";
import { useThreadActions } from "./use-thread-actions";
import { threadActivity } from "./thread-activity";
import { ThreadStatePanel } from "./thread-state-panel";
import { ConversationSkeleton } from "./thread-skeletons";
import { subtreeThreads, threadsWithObjectives, threadTitle } from "./thread-tree";

const RepositoryPanel = lazy(
  () => import("@/features/repository/repository-panel"),
);
const WorkspacePanel = lazy(() => import("./workspace-panel"));
export const chatViews = ["chat", "terminal", "workspace", "changes", "state"] as const;
export type ChatView = (typeof chatViews)[number];
const viewLabels: Record<Exclude<ChatView, "chat">, string> = {
  terminal: "Terminal",
  workspace: "Workspace",
  changes: "Changes",
  state: "State",
};
// Subagent tabs past this count go into the tab bar's "more" menu.
const SUBAGENT_TABS = 4;
const noop = () => {};
const ignoreRefresh = async () => {};

// One thread's conversation and details: the agentmesh console conversation on
// Hatchery's streams. The durable transcript comes from the chat's store and
// refreshes on the chat's event stream; the in-flight turn streams through
// `useChat`. A draft (`chat` is null) persists on first send and keeps this
// component, its composer, and its draft mounted while the thread starts.
export function Conversation({
  chatId,
  chat,
  agents,
  agentId,
  roster,
  warning,
  leading,
  view: routedView,
  onViewChange,
  composerRef,
  onPersist,
  refreshRoster = ignoreRefresh,
  onChatChanged = noop,
  onChatUpdated = noop,
  onAgentChange,
  onArchiveChange = noop,
  onAgentAssigned = noop,
  onThread = noop,
  onNewThread = noop,
  onSent = noop,
}: {
  chatId: string;
  chat: Chat | null;
  agents: Agent[];
  agentId: string | null;
  roster?: AgentThreads;
  warning?: string;
  leading?: ReactNode;
  // The shown tab; the conversation keeps its own when this is not given.
  view?: ChatView;
  onViewChange?: (view: ChatView) => void;
  composerRef?: Ref<HTMLTextAreaElement>;
  onPersist?: (agentId: string | null) => Promise<Chat>;
  refreshRoster?: () => Promise<unknown>;
  onChatChanged?: () => void;
  onChatUpdated?: (chat: Chat) => void;
  onAgentChange: (agentId: string) => void | Promise<void>;
  onArchiveChange?: (archived: boolean) => void;
  onAgentAssigned?: (agentId: string) => void;
  onThread?: (threadId: string) => void;
  onNewThread?: () => void;
  // A message was accepted: the sidebar wakes this thread's card at once.
  onSent?: () => void;
}) {
  // A session that began as a draft owns its first stream: nothing to load or
  // resume, and the first stream announcement is its own.
  const [startedHere] = useState(chat === null);
  const persisted = chat !== null;
  const transport = useMemo(
    () =>
      new DefaultChatTransport<ChatUIMessage>({
        api: "/api/chat",
        credentials: "include",
        prepareSendMessagesRequest: ({ id, messages }) => ({
          body: { chat_id: id, messages },
        }),
        prepareReconnectToStreamRequest: ({ id }) => ({
          api: `/api/chat/${id}/stream`,
          credentials: "include",
        }),
      }),
    [],
  );
  const {
    messages: streamed,
    setMessages,
    sendMessage,
    resumeStream,
    status,
    stop: detach,
    error: streamError,
  } = useChat<ChatUIMessage>({
    id: chatId,
    transport,
    messages: [],
    resume: !startedHere,
  });

  const [transcript, setTranscript] = useState<Message[] | null>(
    startedHere ? [] : null,
  );
  // A finished live turn gives way to its saved messages once a transcript
  // load that began after the stream ended has arrived.
  const streaming = status === "submitted" || status === "streaming";
  const [wasStreaming, setWasStreaming] = useState(false);
  const [ended, setEnded] = useState(0);
  if (streaming !== wasStreaming) {
    setWasStreaming(streaming);
    if (!streaming) setEnded(ended + 1);
  }
  const [loadedAt, setLoadedAt] = useState(-1);
  const endedRef = useRef(ended);
  useEffect(() => {
    endedRef.current = ended;
  }, [ended]);
  const loadTranscript = useCallback(async () => {
    const generation = endedRef.current;
    try {
      const response = await apiFetch(`/api/chats/${chatId}/transcript`);
      if (!response.ok) return;
      setTranscript(await response.json());
      setLoadedAt(generation);
    } catch {}
  }, [chatId]);
  // Loads on mount and again whenever a stream ends.
  useEffect(() => {
    if (persisted) void Promise.resolve().then(loadTranscript);
  }, [ended, loadTranscript, persisted]);
  const settled = !streaming && ended > 0 && loadedAt >= ended;
  useEffect(() => {
    if (settled && streamed.length) setMessages([]);
  }, [setMessages, settled, streamed.length]);
  useEffect(() => () => void detach(), [detach]);

  const {
    data: detail,
    error: loadError,
    mutate,
  } = useThread(persisted ? chatId : null);
  const revalidate = useCallback(() => {
    void mutate();
    void refreshRoster();
  }, [mutate, refreshRoster]);
  // The running turn's stream stands in for agentmesh's live state events.
  const pendingRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (status !== "streaming" || pendingRefresh.current) return;
    pendingRefresh.current = setTimeout(() => {
      pendingRefresh.current = null;
      revalidate();
    }, 1_500);
  }, [revalidate, status, streamed]);
  useEffect(
    () => () => {
      if (pendingRefresh.current) clearTimeout(pendingRefresh.current);
    },
    [],
  );

  const [sandboxes, setSandboxes] = useState<SandboxWorkspace[]>([]);
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
        const response = await apiFetch(`/api/chats/${chatId}/sandboxes`);
        setSandboxes(response.ok ? await response.json() : []);
      } catch {
        setSandboxes([]);
      }
    } while (reloadSandboxes.current);
    loadingSandboxes.current = false;
  }, [chatId]);

  const [streamGeneration, setStreamGeneration] = useState(0);
  useEffect(() => {
    if (!persisted) return;
    const frame = requestAnimationFrame(() => void loadSandboxes());
    const source = new EventSource(`/api/chats/${chatId}/events`, {
      withCredentials: true,
    });
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as {
        type?: string;
        generation?: number;
        state?: string;
      };
      if (event.type === "chat.changed") {
        onChatChanged();
        revalidate();
      }
      if (
        event.type === "sandbox.changed" ||
        (event.type === "task.changed" &&
          ["pending", "attention", "complete", "errored", "cancelled"].includes(
            event.state ?? "",
          ))
      ) {
        void loadSandboxes();
      }
      if (event.type === "messages.changed") {
        void loadTranscript();
        revalidate();
      }
      if (
        event.type === "stream.available" &&
        typeof event.generation === "number"
      ) {
        const announced = event.generation + 1;
        setStreamGeneration((current) => Math.max(current, announced));
        revalidate();
      }
    };
    return () => {
      cancelAnimationFrame(frame);
      source.close();
    };
  }, [chatId, loadSandboxes, loadTranscript, onChatChanged, persisted, revalidate]);

  const attachedGeneration = useRef(streamGeneration);
  const suppressNextStream = useRef(startedHere);
  useEffect(() => {
    const action = streamAttachmentAction(
      attachedGeneration.current,
      streamGeneration,
      status,
      suppressNextStream.current,
    );
    if (action === "ignore") return;
    if (action === "wait") {
      suppressNextStream.current = true;
      return;
    }
    attachedGeneration.current = streamGeneration;
    suppressNextStream.current = false;
    if (action === "resume") void resumeStream();
  }, [resumeStream, status, streamGeneration]);

  useEffect(() => {
    const assignment = streamed
      .flatMap((message) => message.parts)
      .findLast(
        (part) =>
          part.type === "data-agent-assignment" && part.data.state === "assigned",
      );
    if (assignment?.type === "data-agent-assignment" && assignment.data.agent_id)
      onAgentAssigned(assignment.data.agent_id);
  }, [onAgentAssigned, streamed]);

  const agentThreads = useMemo(() => threadsWithObjectives(roster), [roster]);
  const waitingForBudget = Boolean(
    detail?.activity?.awaiting_admission &&
      (roster?.waiting.includes(detail.thread_id) ||
        (detail.activity.budget_held && roster?.budget?.exhausted)),
  );
  const stateDetail =
    detail?.activity && waitingForBudget
      ? {
          ...detail,
          status: "waiting",
          activity: { ...detail.activity, budget_held: true },
        }
      : detail;
  const threadId = detail?.thread_id ?? "";
  const directChildren = useMemo(
    () =>
      threadId
        ? agentThreads.filter((thread) => thread.parent_thread_id === threadId)
        : [],
    [agentThreads, threadId],
  );
  const hierarchyThreads = stateDetail
    ? [
        ...agentThreads.filter((thread) => thread.thread_id !== threadId),
        {
          ...stateDetail,
          task_objective: agentThreads.find(
            (thread) => thread.thread_id === threadId,
          )?.task_objective,
        },
      ]
    : agentThreads;
  const parentThread = agentThreads.find(
    (thread) => thread.thread_id === detail?.parent_thread_id,
  );
  const detailActivity = stateDetail ? threadActivity(stateDetail) : null;

  const [queued, setQueued] = useState<PendingPrompt[]>([]);
  const durableIds = useMemo(
    () => new Set((transcript ?? []).flatMap((m) => (m.id ? [m.id] : []))),
    [transcript],
  );
  const lastStreamedUser = streamed.findLast((m) => m.role === "user")?.id;
  const queuedPrompts = queued.filter(
    (prompt) =>
      !durableIds.has(prompt.requestId) &&
      // Its turn began once the stream for it produces output.
      !(status === "streaming" && lastStreamedUser === prompt.requestId),
  );
  const hiddenKey = queuedPrompts.map((prompt) => prompt.requestId).join("\n");
  // The first message of a draft shows while its chat is being created.
  const [firstMessage, setFirstMessage] = useState<Message | null>(null);
  const live = useMemo(() => {
    const pending =
      firstMessage &&
      !durableIds.has(firstMessage.id!) &&
      !streamed.some((message) => message.id === firstMessage.id)
        ? [firstMessage]
        : [];
    if (settled) return pending;
    const hidden = new Set(hiddenKey.split("\n"));
    return [
      ...pending,
      ...liveMessages(
        streamed.filter(
          (message) => !durableIds.has(message.id) && !hidden.has(message.id),
        ),
      ),
    ];
  }, [durableIds, firstMessage, hiddenKey, settled, streamed]);
  const messages = useMemo(
    () => [...(transcript ?? []), ...live],
    [transcript, live],
  );

  const [persisting, setPersisting] = useState(false);
  const { stop, resume, approve, error, resuming, stopping } =
    useThreadActions({ chatId, refresh: refreshRoster, mutate });
  const working = Boolean(detailActivity?.working) || streaming;
  const responding = Boolean(
    status === "submitted" ||
      persisting ||
      (detail?.live &&
        !detailActivity?.budgetHeld &&
        (detailActivity?.working ||
          detail.activity?.mailbox_depth ||
          detail.activity?.awaiting_admission)),
  );

  const [sending, setSending] = useState("");
  async function send({ text }: { text: string }) {
    setSending("");
    const message = newChatHandoff(text).message;
    if (!persisted) {
      if (!onPersist) return false;
      setFirstMessage({
        role: "user",
        id: message.id,
        request_id: message.id,
        pending: "sending",
        timestamp: Date.now() / 1000,
        parts: [{ kind: "text", id: `${message.id}:0`, text }],
      });
      setPersisting(true);
      try {
        await onPersist(agentId);
      } catch (reason) {
        setFirstMessage(null);
        setSending(reason instanceof Error ? reason.message : "Could not create chat");
        return false;
      } finally {
        setPersisting(false);
      }
    } else if (working || queuedPrompts.length) {
      setQueued((current) => [
        ...current,
        { requestId: message.id, text, timestamp: Date.now() / 1000 },
      ]);
    }
    void sendMessage(message);
    onSent();
    return true;
  }

  const [markingSeen, setMarkingSeen] = useState(false);
  const [seenError, setSeenError] = useState("");
  async function markAsSeen() {
    setMarkingSeen(true);
    setSeenError("");
    try {
      const response = await apiFetch(`/api/chats/${chatId}/seen`, {
        method: "POST",
      });
      if (!response.ok) throw new Error();
      onChatUpdated(await response.json());
    } catch {
      setSeenError("Could not mark this chat as seen.");
    } finally {
      setMarkingSeen(false);
    }
  }

  const [ownView, setOwnView] = useState<ChatView>("chat");
  const view = routedView ?? ownView;
  const selectView = (next: ChatView) =>
    onViewChange ? onViewChange(next) : setOwnView(next);
  const sidebarId = useId();
  const [selectedProposal, setSelectedProposal] = useState("");
  const changes = useApi<Repository>(
    detail?.base_sha && detail.checkpoint_sha
      ? `/api/repository?chat_id=${encodeURIComponent(chatId)}&revision=${detail.checkpoint_sha}`
      : null,
  );
  const hasChanges = Boolean(
    detail?.proposals.length || changes.data?.changes.length,
  );
  // A draft has only its chat; the rest appear once the chat exists.
  const views: ChatView[] = persisted
    ? [
        "chat",
        ...(sandboxes.length ? (["terminal"] as const) : []),
        "workspace",
        ...(hasChanges ? (["changes"] as const) : []),
        "state",
      ]
    : ["chat"];
  const activeView = views.includes(view) ? view : "chat";
  const proposal = detail?.proposals.find((p) => p.branch === selectedProposal);

  const scrollRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  useEffect(() => {
    const element = scrollRef.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [messages, responding]);

  const archived = Boolean(chat?.archived_at);
  const title =
    (chat && !chat.parent_chat_id ? chatSidebarText(chat).label : "") ||
    (detail
      ? threadTitle(
          hierarchyThreads.find((thread) => thread.thread_id === threadId) ??
            detail,
        )
      : "") ||
    (startedHere ? "New thread" : "Conversation");
  const agentName =
    agents.find((agent) => agent.id === agentId)?.name ?? "the agent";
  const changesFallback = startedHere ? (
    <EmptyChanges />
  ) : (
    <RepositoryPanelSkeleton compact />
  );
  const statusLabel = stateDetail ? threadActivity(stateDetail).label : "";

  // Tabs for the chat's thread family: the top chat, then its subagents with a
  // chat, then views of the thread shown now.
  // Roster order, so tabs keep the sidebar's order.
  const family = agentThreads.some((item) => item.thread_id === threadId)
    ? agentThreads
    : hierarchyThreads;
  const familyById = new Map(family.map((item) => [item.thread_id, item]));
  let rootThread = threadId ? familyById.get(threadId) : undefined;
  const walked = new Set<string>();
  while (rootThread?.parent_thread_id && !walked.has(rootThread.thread_id)) {
    walked.add(rootThread.thread_id);
    const parent = familyById.get(rootThread.parent_thread_id);
    if (!parent) break;
    rootThread = parent;
  }
  const subagents = rootThread
    ? subtreeThreads(family, rootThread.thread_id)
        .slice(1)
        .filter((item) => item.chat_id && (!item.archived || item.thread_id === threadId))
    : [];
  const shownSubagents = subagents.filter(
    (item, index) => index < SUBAGENT_TABS || item.thread_id === threadId,
  );
  const threadTab = (thread: Thread | undefined, label: string): ContextTab =>
    !thread || thread.thread_id === threadId
      ? {
          key: thread?.thread_id ?? "chat",
          label,
          title,
          id: `${sidebarId}-chat-tab`,
          controls: `${sidebarId}-chat-panel`,
          selected: activeView === "chat",
          current: true,
          onSelect: () => selectView("chat"),
        }
      : {
          key: thread.thread_id,
          label,
          title: threadTitle(thread),
          selected: false,
          onSelect: () => onThread(thread.thread_id),
        };
  const tabs: ContextTab[] = [
    threadTab(rootThread, "Chat"),
    ...shownSubagents.map((item) => threadTab(item, threadTitle(item))),
    ...views
      .filter((item) => item !== "chat")
      .map((item, index) => ({
        key: item,
        label: viewLabels[item],
        id: `${sidebarId}-${item}-tab`,
        controls: `${sidebarId}-${item}-panel`,
        selected: activeView === item,
        separated: index === 0,
        onSelect: () => selectView(item),
      })),
  ];
  const moreTabs = subagents
    .filter((item) => !shownSubagents.includes(item))
    .map((item) => threadTab(item, threadTitle(item)));

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Thread">
      <ContextTabs label="Chat" leading={leading} tabs={tabs} more={moreTabs} />
      <Activity mode={activeView === "chat" ? "visible" : "hidden"}>
        <div
          id={`${sidebarId}-chat-panel`}
          role="tabpanel"
          aria-labelledby={`${sidebarId}-chat-tab`}
          className="flex min-h-0 min-w-0 flex-1 flex-col"
        >
          <header className="flex h-12 shrink-0 items-center gap-3 border-b px-4">
            <h2
              className="min-w-0 flex-1 truncate text-sm font-medium"
              title={title}
            >
              {title}
            </h2>
            {stateDetail ? (
              <span
                className="text-xs text-muted-foreground"
                title={`${stateDetail.turns} turns · ${number(stateDetail.input_tokens + stateDetail.output_tokens)} tokens`}
              >
                {statusLabel === "Ready for your reply" ? "" : statusLabel}
              </span>
            ) : null}
            {persisted &&
            !archived &&
            detail?.live &&
            detail.status === "idle" &&
            !queuedPrompts.length &&
            !working &&
            !detail.activity?.mailbox_depth ? (
              <Button
                size="sm"
                variant="ghost"
                title="Archive this idle thread"
                onClick={() => onArchiveChange(true)}
              >
                <Check />
                Archive
              </Button>
            ) : null}
            {archived ? (
              <Button size="sm" variant="ghost" onClick={() => onArchiveChange(false)}>
                Unarchive
              </Button>
            ) : null}
            {detail?.status === "parked" ? (
              <Button size="sm" disabled={resuming} onClick={() => void resume()}>
                {resuming ? "Resuming…" : "Resume"}
              </Button>
            ) : null}
          </header>
          {error || loadError || detail?.error || sending || seenError ? (
            <p
              role="alert"
              className="shrink-0 border-b px-5 py-3 text-sm text-destructive"
            >
              {error || loadError?.message || detail?.error || sending || seenError}
            </p>
          ) : null}
          <div
            ref={scrollRef}
            className="min-h-0 flex-1 overflow-y-auto overscroll-contain [overflow-anchor:none]"
            aria-label="Messages"
            role="region"
            tabIndex={0}
            onScroll={(event) => {
              const element = event.currentTarget;
              following.current =
                element.scrollHeight - element.scrollTop - element.clientHeight <
                80;
            }}
          >
            <div
              className={`mx-auto w-full max-w-3xl px-5 py-8 sm:px-7 ${!messages.length && !responding ? "grid h-full min-h-48 place-content-center" : ""}`}
            >
              {warning ? (
                <Alert className="mb-6">
                  <TriangleAlertIcon />
                  <AlertTitle>GitHub access needed</AlertTitle>
                  <AlertDescription>{warning}</AlertDescription>
                </Alert>
              ) : null}
              {transcript === null ? (
                <ConversationSkeleton />
              ) : !messages.length && !responding ? (
                <div className="space-y-2 text-center">
                  <h3 className="text-xl font-medium tracking-tight">
                    What are we working on?
                  </h3>
                  <p className="text-sm text-muted-foreground">
                    {agentId
                      ? `Message ${agentName} to start a thread.`
                      : "Send a message; Hatchery picks the agent."}
                  </p>
                </div>
              ) : (
                <Transcript
                  messages={messages}
                  owner={agentId ?? undefined}
                  responding={responding}
                  respondingLabel={
                    status === "submitted" ? submissionLabel(agentId) : undefined
                  }
                  toolProgress={detail?.tool_progress}
                  stopped={detail ? !detail.live : false}
                  threads={directChildren}
                  parentThread={parentThread}
                  parentThreadId={detail?.parent_thread_id}
                  onThread={onThread}
                />
              )}
              {roster?.budget?.exhausted && detailActivity?.budgetHeld ? (
                <BudgetHoldNotice
                  agentId={roster.agent_id}
                  budget={roster.budget}
                  heldCount={roster.waiting.length}
                  onGranted={async () => {
                    await Promise.all([refreshRoster(), mutate()]);
                  }}
                />
              ) : null}
              {streamError ? (
                <p role="status" className="mt-4 text-xs text-muted-foreground">
                  {streamError.message}
                </p>
              ) : null}
            </div>
          </div>
          <div className="mx-auto w-full max-w-3xl shrink-0 p-4 pt-2 sm:px-6 sm:pb-5">
            {archived ? (
              <Alert>
                <AlertTitle>This chat is archived</AlertTitle>
                <AlertDescription className="flex items-center justify-between gap-3">
                  <span>Unarchive it before posting.</span>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => onArchiveChange(false)}
                  >
                    Unarchive
                  </Button>
                </AlertDescription>
              </Alert>
            ) : detail && !detail.live ? (
              <div className="flex items-center justify-between gap-3 rounded-xl bg-muted/40 px-4 py-3">
                <p className="text-xs text-muted-foreground">Thread finished</p>
                <Button variant="ghost" size="sm" onClick={onNewThread}>
                  Start a new thread
                </Button>
              </div>
            ) : (
              <>
                <QueuedPrompts prompts={queuedPrompts} />
                <PromptForm
                  ref={composerRef}
                  autoFocus={startedHere}
                  isBusy={working}
                  sendDisabled={stopping || persisting}
                  traceId={chat?.telemetry_span?.trace_id ?? null}
                  agents={agents}
                  agentId={detail ? null : agentId}
                  showAutoAgent={agentId === null && !detail}
                  showMarkAsRead={Boolean(chat?.attention_reason)}
                  isMarkingAsRead={markingSeen}
                  onSubmit={send}
                  onStop={async () => {
                    void detach();
                    if (detail) await stop();
                  }}
                  onAgentChange={onAgentChange}
                  onMarkAsRead={() => void markAsSeen()}
                />
              </>
            )}
          </div>
        </div>
      </Activity>
        {hasChanges ? (
          <Activity mode={activeView === "changes" ? "visible" : "hidden"}>
            <div
              id={`${sidebarId}-changes-panel`}
              role="tabpanel"
              aria-labelledby={`${sidebarId}-changes-tab`}
              className="flex min-h-0 flex-1 flex-col"
            >
              {detail?.proposals.length ? (
                <div className="shrink-0 space-y-2 border-b px-3 py-3">
                  <nav aria-label="Review source" className="flex flex-wrap gap-1">
                    <Button
                      size="sm"
                      variant={!proposal ? "secondary" : "ghost"}
                      aria-pressed={!proposal}
                      onClick={() => setSelectedProposal("")}
                    >
                      All thread edits
                    </Button>
                    {detail.proposals.map((item) => (
                      <Button
                        key={item.branch}
                        size="sm"
                        variant={
                          proposal?.branch === item.branch ? "secondary" : "ghost"
                        }
                        aria-pressed={proposal?.branch === item.branch}
                        onClick={() => setSelectedProposal(item.branch)}
                      >
                        {item.section === "wiki"
                          ? "Wiki proposal"
                          : "Workspace proposal"}
                      </Button>
                    ))}
                  </nav>
                  <p className="px-1 text-xs text-muted-foreground">
                    {proposal
                      ? "Changes selected to save to main."
                      : "All edits since this thread started."}
                  </p>
                </div>
              ) : null}
              {detail?.base_sha ? (
                <Suspense fallback={changesFallback}>
                  <RepositoryPanel
                    key={proposal?.branch ?? chatId}
                    chatId={chatId}
                    proposal={proposal?.branch}
                    revision={proposal ? undefined : detail.checkpoint_sha || undefined}
                    compact
                    loadingFallback={startedHere ? <EmptyChanges /> : undefined}
                    onApprove={
                      proposal && roster?.local_review && !detail.parent_thread_id
                        ? (sha) => approve(proposal.branch, sha)
                        : undefined
                    }
                    reviewUrl={proposal?.url}
                  />
                </Suspense>
              ) : !detail && !startedHere ? (
                changesFallback
              ) : (
                <EmptyChanges />
              )}
            </div>
          </Activity>
        ) : null}
        <Activity mode={activeView === "workspace" ? "visible" : "hidden"}>
          <div
            id={`${sidebarId}-workspace-panel`}
            role="tabpanel"
            aria-labelledby={`${sidebarId}-workspace-tab`}
            className="flex min-h-0 flex-1 flex-col"
          >
            {detail ? (
              <Suspense
                fallback={
                  <p role="status" className="p-5 text-sm text-muted-foreground">
                    Loading workspace…
                  </p>
                }
              >
                <WorkspacePanel
                  key={chatId}
                  chatId={chatId}
                  visible={activeView === "workspace"}
                  waitForCreation={startedHere && !detail.base_sha}
                />
              </Suspense>
            ) : (
              <p role="status" className="p-5 text-sm text-muted-foreground">
                {persisted
                  ? "Starting thread…"
                  : "Send a message to start a thread."}
              </p>
            )}
          </div>
        </Activity>
        {activeView === "state" ? (
          <div
            id={`${sidebarId}-state-panel`}
            role="tabpanel"
            aria-labelledby={`${sidebarId}-state-tab`}
            className="flex min-h-0 flex-1 flex-col"
          >
            {!detail && !loadError ? (
              <p role="status" className="p-5 text-sm text-muted-foreground">
                {persisted
                  ? "Starting thread…"
                  : "Send a message to start a thread."}
              </p>
            ) : (
              <ThreadStatePanel
                thread={stateDetail}
                threads={hierarchyThreads}
                onSelect={onThread}
                error={loadError?.message}
              />
            )}
          </div>
        ) : null}
        {sandboxes.length ? (
          <Activity mode={activeView === "terminal" ? "visible" : "hidden"}>
            <div
              id={`${sidebarId}-terminal-panel`}
              role="tabpanel"
              aria-labelledby={`${sidebarId}-terminal-tab`}
              className="flex min-h-0 flex-1 flex-col"
            >
              <TerminalPane
                chatId={chatId}
                sandboxes={sandboxes}
                onChanged={() => void loadSandboxes()}
              />
            </div>
          </Activity>
        ) : null}
    </section>
  );
}
