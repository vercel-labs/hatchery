import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useMemo, useRef, useState } from "react";
import { PlusIcon } from "lucide-react";

import { ChatMessage } from "@/components/chat-message";
import { isBrandNewChat } from "@/components/new-chat-state";
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
import { apiBase, apiFetch, type Chat, type Space } from "@/lib/api";
import { submissionLabel } from "@/components/chat-status";
import type { ChatUIMessage } from "@/lib/messages";

export function NewChatView({
  spaces,
  onPersist,
  onQueueInitialMessage,
  onOpenChat,
  onCreateSpace,
  isCurrent,
}: {
  spaces: Space[];
  onPersist: (spaceId: string | null) => Promise<Chat>;
  onQueueInitialMessage: (chatId: string, text: string) => void;
  onOpenChat: (chatId: string) => void;
  onCreateSpace: () => void;
  isCurrent: () => boolean;
}) {
  const [spaceId, setSpaceId] = useState<string | null>(null);
  const [showSandboxForm, setShowSandboxForm] = useState(false);
  const [persisting, setPersisting] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);

  const submit = async ({ text }: { text: string }) => {
    if (submitting.current) throw new Error("Chat creation is already in progress");
    submitting.current = true;
    setPersisting(true);
    setError("");
    try {
      const chat = await onPersist(spaceId);
      if (!isCurrent()) return;
      onQueueInitialMessage(chat.id, text);
      onOpenChat(chat.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create chat");
      submitting.current = false;
      setPersisting(false);
      throw reason;
    }
  };

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-6">
      <div className="flex w-full max-w-2xl flex-col gap-3">
        <h1 className="px-1 text-sm font-medium text-muted-foreground">
          New chat
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
          spaces={spaces}
          spaceId={spaceId}
          showAutoSpace
          showMarkAsRead={false}
          isMarkingAsRead={false}
          onSubmit={submit}
          onStop={() => {}}
          onSpaceChange={(nextSpaceId) => setSpaceId(nextSpaceId)}
          onMarkAsRead={() => {}}
        />
        <div className="flex flex-wrap gap-2 px-1">
          <Button variant="outline" onClick={() => setShowSandboxForm(true)}>
            <PlusIcon data-icon="inline-start" />
            Create sandbox manually
          </Button>
          <Button variant="outline" onClick={onCreateSpace}>
            <PlusIcon data-icon="inline-start" />
            New space
          </Button>
        </div>
      </div>
      <SandboxForm
        spaceId={spaceId}
        open={showSandboxForm}
        onOpenChange={setShowSandboxForm}
        onPersist={() => onPersist(spaceId)}
        onCreated={(_sandboxId, chatId) => onOpenChat(chatId)}
        isCurrent={isCurrent}
      />
    </div>
  );
}

export function ChatView({
  chatId,
  initialMessages,
  spaceId,
  spaces,
  messageRevision,
  streamGeneration,
  traceId,
  archived,
  attentionReason,
  initialMessage,
  onInitialMessageStarted,
  onMessagesChange,
  onSeen,
  onSpaceChange,
  onUnarchive,
  onCreateSandbox,
  onCreateSpace,
}: {
  chatId: string;
  initialMessages: ChatUIMessage[];
  spaceId: string | null;
  spaces: Space[];
  messageRevision: number;
  streamGeneration: number;
  traceId: string | null;
  archived: boolean;
  attentionReason: Chat["attention_reason"];
  initialMessage?: string;
  onInitialMessageStarted?: () => void;
  onMessagesChange?: (messages: ChatUIMessage[]) => void;
  onSeen: (chat: Chat) => void;
  onSpaceChange: (spaceId: string) => void | Promise<void>;
  onUnarchive: () => void;
  onCreateSandbox: () => void;
  onCreateSpace: () => void;
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
    resume: true,
  });

  const attachedGeneration = useRef(streamGeneration);
  const initialMessageSent = useRef(false);
  const [markingSeen, setMarkingSeen] = useState(false);
  const [seenError, setSeenError] = useState("");

  useEffect(() => {
    if (!initialMessage || initialMessageSent.current) return;
    initialMessageSent.current = true;
    onInitialMessageStarted?.();
    void sendMessage({ text: initialMessage });
  }, [initialMessage, onInitialMessageStarted, sendMessage]);

  useEffect(() => {
    onMessagesChange?.(messages);
  }, [messages, onMessagesChange]);

  useEffect(() => () => {
    void stop();
  }, [stop]);

  useEffect(() => {
    if (
      streamGeneration > attachedGeneration.current &&
      status === "ready"
    ) {
      attachedGeneration.current = streamGeneration;
      void resumeStream();
    }
  }, [resumeStream, status, streamGeneration]);

  useEffect(() => {
    if (
      messageRevision === 0 ||
      status === "submitted" ||
      status === "streaming"
    ) {
      return;
    }
    apiFetch(`/api/chats/${chatId}/messages`)
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
      const response = await apiFetch(`/api/chats/${chatId}/seen`, {
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
            New chat
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
            spaces={spaces}
            spaceId={spaceId}
            showAutoSpace={spaceId === null}
            showMarkAsRead={false}
            isMarkingAsRead={markingSeen}
            onSubmit={({ text }) => {
              void sendMessage({ text });
            }}
            onStop={() => void stop()}
            onSpaceChange={onSpaceChange}
            onMarkAsRead={() => void markAsSeen()}
          />
          <div className="flex flex-wrap gap-2 px-1">
            <Button variant="outline" onClick={onCreateSandbox}>
              <PlusIcon data-icon="inline-start" />
              Create sandbox manually
            </Button>
            <Button variant="outline" onClick={onCreateSpace}>
              <PlusIcon data-icon="inline-start" />
              New space
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
                      {submissionLabel(spaceId)}
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
            spaces={spaces}
            spaceId={spaceId}
            showMarkAsRead={Boolean(attentionReason)}
            isMarkingAsRead={markingSeen}
            onSubmit={({ text }) => {
              void sendMessage({ text });
            }}
            onStop={() => void stop()}
            onSpaceChange={onSpaceChange}
            onMarkAsRead={() => void markAsSeen()}
          />
        )}
      </div>
    </div>
  );
}
