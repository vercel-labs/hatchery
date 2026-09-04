import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useMemo, useRef, useState } from "react";
import { PlusIcon } from "lucide-react";

import { ChatMessage } from "@/components/chat-message";
import { Button } from "@/components/ui/button";
import { PromptForm } from "@/components/prompt-form";
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
import { apiBase, apiFetch, type Chat } from "@/lib/api";
import { submissionLabel } from "@/components/chat-status";
import type { ChatUIMessage } from "@/lib/messages";

export function ChatView({
  chatId,
  initialMessages,
  spaceId,
  messageRevision,
  streamGeneration,
  archived,
  attentionReason,
  onMessagesChange,
  onSeen,
  onUnarchive,
  onCreateSandbox,
}: {
  chatId: string;
  initialMessages: ChatUIMessage[];
  spaceId: string | null;
  messageRevision: number;
  streamGeneration: number;
  archived: boolean;
  attentionReason: Chat["attention_reason"];
  onMessagesChange?: (messages: ChatUIMessage[]) => void;
  onSeen: (chat: Chat) => void;
  onUnarchive: () => void;
  onCreateSandbox: () => void;
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
  const [markingSeen, setMarkingSeen] = useState(false);
  const [seenError, setSeenError] = useState("");

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
        {attentionReason && (
          <div className="flex justify-end">
            <Button
              size="sm"
              variant="outline"
              disabled={markingSeen}
              onClick={markAsSeen}
            >
              {markingSeen ? "Marking as seen…" : "Mark as seen"}
            </Button>
          </div>
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
            onSubmit={({ text }) => sendMessage({ text })}
            onStop={() => void stop()}
          />
        )}
      </div>
    </div>
  );
}
