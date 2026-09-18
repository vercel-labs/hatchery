import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useMemo, useRef, useState } from "react";
import { PlusIcon } from "lucide-react";

import { ChatMessage } from "@/components/chat-message";
import {
  isBrandNewChat,
  newChatHandoff,
  streamAttachmentAction,
  type NewChatHandoff,
} from "@/components/new-chat-state";
import { Button } from "@/components/ui/button";
import { PromptForm } from "@/components/prompt-form";
import { SandboxForm } from "@/components/sandbox-form";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import { apiBase, apiFetch, type Thread, type Agent } from "@/lib/api";
import { submissionLabel } from "@/components/chat-status";
import type { ChatUIMessage } from "@/lib/messages";

export function NewChatView({
  agents,
  onPersist,
  onHandoff,
  onOpenChat,
  onCreateAgent,
  isCurrent,
}: {
  agents: Agent[];
  onPersist: (agentId: string | null) => Promise<Thread>;
  onHandoff: (chat: Thread, handoff: NewChatHandoff) => void;
  onOpenChat: (chatId: string) => void;
  onCreateAgent: () => void;
  isCurrent: () => boolean;
}) {
  const [agentId, setAgentId] = useState<string | null>(null);
  const [showSandboxForm, setShowSandboxForm] = useState(false);
  const [persisting, setPersisting] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const handoff = useRef<NewChatHandoff | null>(null);

  const submit = async ({ text }: { text: string }) => {
    if (submitting.current) throw new Error("Thread creation is already in progress");
    submitting.current = true;
    setPersisting(true);
    setError("");
    try {
      handoff.current ??= newChatHandoff(text);
      const chat = await onPersist(agentId);
      if (!isCurrent()) return;
      onHandoff(chat, handoff.current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create thread");
      submitting.current = false;
      setPersisting(false);
      throw reason;
    }
  };

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-6">
      <div className="flex w-full max-w-2xl flex-col gap-3">
        <h1 className="px-1 text-sm font-medium text-muted-foreground">
          New thread
        </h1>
        {error && (
          <Alert variant="destructive">
            <AlertTitle>Request failed</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <PromptForm
          autoFocus
          isBusy={persisting}
          traceId={null}
          agents={agents}
          agentId={agentId}
          showAutoAgent
          showMarkAsRead={false}
          isMarkingAsRead={false}
          onSubmit={submit}
          onStop={() => {}}
          onAgentChange={(nextAgentId) => setAgentId(nextAgentId)}
          onMarkAsRead={() => {}}
        />
        <div className="flex flex-wrap gap-2 px-1">
          <Button variant="outline" onClick={() => setShowSandboxForm(true)}>
            <PlusIcon data-icon="inline-start" />
            Create sandbox manually
          </Button>
          <Button variant="outline" onClick={onCreateAgent}>
            <PlusIcon data-icon="inline-start" />
            New agent
          </Button>
        </div>
      </div>
      <SandboxForm
        agentId={agentId}
        open={showSandboxForm}
        onOpenChange={setShowSandboxForm}
        onPersist={() => onPersist(agentId)}
        onCreated={(_sandboxId, chatId) => onOpenChat(chatId)}
        isCurrent={isCurrent}
      />
    </div>
  );
}

export function ChatView({
  chatId,
  initialMessages,
  agentId,
  agents,
  messageRevision,
  streamGeneration,
  traceId,
  archived,
  attentionReason,
  handoff,
  onMessagesChange,
  onSeen,
  onAgentChange,
  onUnarchive,
  onCreateSandbox,
  onCreateAgent,
}: {
  chatId: string;
  initialMessages: ChatUIMessage[];
  agentId: string | null;
  agents: Agent[];
  messageRevision: number;
  streamGeneration: number;
  traceId: string | null;
  archived: boolean;
  attentionReason: Thread["attention_reason"];
  handoff?: NewChatHandoff;
  onMessagesChange?: (messages: ChatUIMessage[]) => void;
  onSeen: (chat: Thread) => void;
  onAgentChange: (agentId: string) => void | Promise<void>;
  onUnarchive: () => void;
  onCreateSandbox: () => void;
  onCreateAgent: () => void;
}) {
  const transport = useMemo(
    () =>
      new DefaultChatTransport<ChatUIMessage>({
        api: `${apiBase()}/api/chat`,
        credentials: "include",
        prepareSendMessagesRequest: ({ id, messages }) => {
          return { body: { chat_id: id, messages } };
        },
        prepareReconnectToStreamRequest: ({ id }) => ({
          api: `${apiBase()}/api/chat/${id}/stream`,
          credentials: "include",
        }),
      }),
    [],
  );

  const [startup] = useState(handoff);
  const {
    messages,
    setMessages,
    sendMessage,
    resumeStream,
    status,
    stop,
    error,
  } = useChat<ChatUIMessage>({
    id: chatId,
    transport,
    messages: initialMessages,
    resume: startup?.resumeOnMount ?? true,
  });

  const attachedGeneration = useRef(streamGeneration);
  const suppressNextStream = useRef(startup?.suppressNextStream ?? false);
  const initialMessageSent = useRef(false);
  const [markingSeen, setMarkingSeen] = useState(false);
  const [seenError, setSeenError] = useState("");

  useEffect(() => {
    if (!startup || initialMessageSent.current) return;
    const timeout = window.setTimeout(() => {
      initialMessageSent.current = true;
      void sendMessage(startup.message);
    });
    return () => window.clearTimeout(timeout);
  }, [sendMessage, startup]);

  useEffect(() => {
    onMessagesChange?.(messages);
  }, [messages, onMessagesChange]);

  useEffect(() => () => {
    void stop();
  }, [stop]);

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
    if (
      messageRevision === 0 ||
      status === "submitted" ||
      status === "streaming"
    ) {
      return;
    }
    apiFetch(`/api/threads/${chatId}/messages`)
      .then((response) => (response.ok ? response.json() : null))
      .then((stored: ChatUIMessage[] | null) => {
        if (stored) setMessages(stored);
      })
      .catch(() => {});
  }, [chatId, messageRevision, setMessages, status]);

  const isStreaming = status === "submitted" || status === "streaming";

  const markAsSeen = async () => {
    setMarkingSeen(true);
    setSeenError("");
    try {
      const response = await apiFetch(`/api/threads/${chatId}/seen`, {
        method: "POST",
      });
      if (!response.ok) throw new Error();
      onSeen(await response.json());
    } catch {
      setSeenError("Could not mark this chat as seen.");
    } finally {
      setMarkingSeen(false);
    }
  };

  const brandNew = isBrandNewChat(messages.length);
  if (brandNew && !archived) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6">
        <div className="flex w-full max-w-2xl flex-col gap-3">
          <h1 className="px-1 text-sm font-medium text-muted-foreground">
            New thread
          </h1>
          {error && (
            <Alert variant="destructive">
              <AlertTitle>Request failed</AlertTitle>
              <AlertDescription>{error.message}</AlertDescription>
            </Alert>
          )}
          <PromptForm
            autoFocus
            isBusy={isStreaming}
            traceId={traceId}
            agents={agents}
            agentId={agentId}
            showAutoAgent={agentId === null}
            showMarkAsRead={false}
            isMarkingAsRead={markingSeen}
            onSubmit={({ text }) => {
              void sendMessage({ text });
            }}
            onStop={() => void stop()}
            onAgentChange={onAgentChange}
            onMarkAsRead={() => void markAsSeen()}
          />
          <div className="flex flex-wrap gap-2 px-1">
            <Button variant="outline" onClick={onCreateSandbox}>
              <PlusIcon data-icon="inline-start" />
              Create sandbox manually
            </Button>
            <Button variant="outline" onClick={onCreateAgent}>
              <PlusIcon data-icon="inline-start" />
              New agent
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex min-h-0 w-full flex-1 flex-col">
      {messages.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <Empty>
            <EmptyHeader>
              <EmptyTitle>Talk to the dispatcher</EmptyTitle>
              <EmptyDescription>
                Describe the work. The dispatcher can start fx subagents.
              </EmptyDescription>
            </EmptyHeader>
            {!archived && (
              <Button variant="outline" onClick={onCreateSandbox}>
                <PlusIcon />
                Create sandbox manually
              </Button>
            )}
          </Empty>
        </div>
      ) : (
        <MessageScrollerProvider
          autoScroll
          defaultScrollPosition="end"
          scrollEdgeThreshold={64}
        >
          <MessageScroller className="flex-1">
            <MessageScrollerViewport>
              <MessageScrollerContent className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-6 py-6">
                {messages.map((message) => (
                  <MessageScrollerItem
                    key={message.id}
                    messageId={message.id}
                    scrollAnchor={message.role === "user"}
                  >
                    <ChatMessage message={message} />
                  </MessageScrollerItem>
                ))}
                {status === "submitted" && (
                  <MessageScrollerItem messageId="thinking">
                    <div className="flex animate-pulse items-center gap-2 px-3 text-sm text-muted-foreground">
                      {submissionLabel(agentId)}
                    </div>
                  </MessageScrollerItem>
                )}
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton />
          </MessageScroller>
        </MessageScrollerProvider>
      )}

      <div className="mx-auto flex w-full max-w-2xl flex-col gap-2 px-6 pb-6">
        {error && (
          <Alert variant="destructive">
            <AlertTitle>Request failed</AlertTitle>
            <AlertDescription>{error.message}</AlertDescription>
          </Alert>
        )}
        {seenError && (
          <Alert variant="destructive">
            <AlertTitle>Request failed</AlertTitle>
            <AlertDescription>{seenError}</AlertDescription>
          </Alert>
        )}
        {archived ? (
          <Alert>
            <AlertTitle>This chat is archived</AlertTitle>
            <AlertDescription className="flex items-center justify-between gap-3">
              <span>Unarchive it before posting or creating a sandbox.</span>
              <Button size="sm" variant="outline" onClick={onUnarchive}>
                Unarchive
              </Button>
            </AlertDescription>
          </Alert>
        ) : (
          <PromptForm
            isBusy={isStreaming}
            traceId={traceId}
            agents={agents}
            agentId={agentId}
            showMarkAsRead={Boolean(attentionReason)}
            isMarkingAsRead={markingSeen}
            onSubmit={({ text }) => {
              void sendMessage({ text });
            }}
            onStop={() => void stop()}
            onAgentChange={onAgentChange}
            onMarkAsRead={() => void markAsSeen()}
          />
        )}
      </div>
    </div>
  );
}
