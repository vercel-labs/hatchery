import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useMemo } from "react";

import { ChatMessage } from "@/components/chat-message";
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
import { apiBase } from "@/lib/api";
import type { ChatUIMessage } from "@/lib/messages";

// Trimmed port of seal's ChatView: no uploads, models, resume, or approvals.
// Keyed by chatId at the call site so it remounts on chat switch;
// initialMessages is the stored transcript, fetched before mount.
export function ChatView({
  chatId,
  initialMessages,
  onMessagesChange,
}: {
  chatId: string;
  initialMessages: ChatUIMessage[];
  onMessagesChange?: (messages: ChatUIMessage[]) => void;
}) {
  const transport = useMemo(
    () =>
      new DefaultChatTransport<ChatUIMessage>({
        api: `${apiBase()}/api/chat`,
        prepareSendMessagesRequest: ({ id, messages }) => {
          return { body: { chat_id: id, messages } };
        },
      }),
    [],
  );

  const { messages, sendMessage, status, stop, error } =
    useChat<ChatUIMessage>({
      id: chatId,
      transport,
      messages: initialMessages,
    });

  useEffect(() => {
    onMessagesChange?.(messages);
  }, [messages, onMessagesChange]);

  const isStreaming = status === "submitted" || status === "streaming";

  return (
    <div className="mx-auto flex min-h-0 w-full flex-1 flex-col">
      {messages.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <Empty>
            <EmptyHeader>
              <EmptyTitle>Talk to the dispatcher</EmptyTitle>
              <EmptyDescription>
                Describe the work; it hands it to a coder you can watch.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        </div>
      ) : (
        <MessageScrollerProvider>
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
                      Thinking…
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
        <PromptForm
          isBusy={isStreaming}
          onSubmit={({ text }) => sendMessage({ text })}
          onStop={() => void stop()}
        />
      </div>
    </div>
  );
}
