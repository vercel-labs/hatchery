import { getToolName, isToolUIPart } from "ai";
import type { ReactNode } from "react";

import { TextPart } from "@/components/parts/text-part";
import { ToolPart } from "@/components/parts/tool-part";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Message, MessageContent } from "@/components/ui/message";
import { getFreshParts } from "@/lib/messages";
import type { ChatMessagePart, ChatUIMessage } from "@/lib/messages";

// Trimmed port of seal's chat-message: text + tool parts only (no files,
// approvals, or subagent recursion — hatchery's worker renders in the
// terminal pane instead).
function renderParts(
  parts: ChatMessagePart[],
  role: ChatUIMessage["role"],
): ReactNode {
  return parts.map((part, index) => {
    if (part.type === "data-space-assignment") {
      return (
        <div key={index} className="px-1.5 text-sm text-muted-foreground">
          {part.data.state === "assigning"
            ? "Assigning a space…"
            : `Assigned ${part.data.space_name ?? "space"}`}
        </div>
      );
    }

    if (isToolUIPart(part)) {
      return (
        <div
          key={index}
          data-tool-name={getToolName(part)}
          data-tool-state={part.state}
          className="flex w-full min-w-0 flex-col gap-1.5 py-0.5"
        >
          <ToolPart part={part} />
        </div>
      );
    }

    if (part.type === "text") {
      return <TextPart key={index} text={part.text} role={role} />;
    }

    return null;
  });
}

export function ChatMessage({ message }: { message: ChatUIMessage }) {
  const parts = getFreshParts(message.parts);

  if (message.role === "user") {
    const text = parts
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join("");

    return (
      <Message align="end">
        <MessageContent>
          {text.trim() && (
            <Bubble align="end" variant="muted" data-message-role="user">
              {message.metadata?.origin === "slack" && (
                <span className="self-end px-1 text-xs text-muted-foreground">
                  via slack
                </span>
              )}
              <BubbleContent>
                <TextPart
                  text={text}
                  role={message.role}
                  preserveLineBreaks
                  className="px-0 [--typeset-size:14px] [--typeset-leading:1.625] [--typeset-flow:0.875em]"
                />
              </BubbleContent>
            </Bubble>
          )}
        </MessageContent>
      </Message>
    );
  }

  return (
    <Message align="start">
      <MessageContent>{renderParts(parts, message.role)}</MessageContent>
    </Message>
  );
}
