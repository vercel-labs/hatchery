import { ArrowUpIcon, SquareIcon } from "lucide-react";
import * as React from "react";

import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AUTO_AGENT_VALUE } from "@/components/new-chat-state";
import { braintrustTraceUrl } from "@/lib/braintrust";
import type { Agent } from "@/lib/api";
import { resolveAgentColor } from "@/lib/agent-colors";

// Trimmed port of seal's prompt-form: text only, no attachments or model
// select.
export function PromptForm({
  isBusy,
  traceId,
  agents,
  agentId,
  showMarkAsRead,
  isMarkingAsRead,
  showAutoAgent = false,
  autoFocus = false,
  onSubmit,
  onStop,
  onAgentChange,
  onMarkAsRead,
}: {
  isBusy: boolean;
  traceId: string | null;
  agents: Agent[];
  agentId: string | null;
  showMarkAsRead: boolean;
  isMarkingAsRead: boolean;
  showAutoAgent?: boolean;
  autoFocus?: boolean;
  onSubmit: (message: { text: string }) => void | Promise<void>;
  onStop: () => void;
  onAgentChange: (agentId: string) => void | Promise<void>;
  onMarkAsRead: () => void;
}) {
  const [input, setInput] = React.useState("");
  const [traceCopied, setTraceCopied] = React.useState(false);
  const [isChangingAgent, setIsChangingAgent] = React.useState(false);
  const selectedAgent = agents.find((agent) => agent.id === agentId);

  async function handleSubmit(event?: React.FormEvent) {
    event?.preventDefault();
    const text = input.trim();
    if (!text || isBusy || isChangingAgent) return;
    try {
      await onSubmit({ text });
      setInput("");
    } catch {}
  }

  return (
    <form onSubmit={handleSubmit}>
      <InputGroup>
        <InputGroupTextarea
          autoFocus={autoFocus}
          placeholder="What should we build?"
          className="p-3.5"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              handleSubmit();
            }
          }}
        />
        <InputGroupAddon align="block-end">
          {(agentId || showAutoAgent) && (
            <Select
              disabled={isChangingAgent}
              value={agentId ?? AUTO_AGENT_VALUE}
              onValueChange={(nextAgentId) => {
                if (nextAgentId && nextAgentId !== AUTO_AGENT_VALUE) {
                  setIsChangingAgent(true);
                  Promise.resolve(onAgentChange(nextAgentId)).finally(() =>
                    setIsChangingAgent(false),
                  );
                }
              }}
            >
              <SelectTrigger
                size="sm"
                aria-label="Thread agent"
                className="h-6 max-w-44 rounded-xl border-transparent bg-secondary px-2 text-secondary-foreground hover:bg-secondary/80"
              >
                <SelectValue>
                  {selectedAgent ? (
                    <>
                      <span
                        className="size-2 shrink-0 rounded-full"
                        style={{
                          backgroundColor: resolveAgentColor(selectedAgent.color),
                        }}
                      />
                      <span className="truncate">{selectedAgent.name}</span>
                    </>
                  ) : (
                    <span className="truncate">Auto</span>
                  )}
                </SelectValue>
              </SelectTrigger>
              <SelectContent align="start">
                <SelectGroup>
                  {showAutoAgent && (
                    <SelectItem value={AUTO_AGENT_VALUE}>Auto</SelectItem>
                  )}
                  {agents.map((agent) => (
                    <SelectItem key={agent.id} value={agent.id}>
                      <span
                        className="size-2 shrink-0 rounded-full"
                        style={{ backgroundColor: resolveAgentColor(agent.color) }}
                      />
                      {agent.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          )}
          {traceId && (
            <Tooltip>
              <TooltipTrigger
                render={
                  <InputGroupButton
                    type="button"
                    size="icon-xs"
                    aria-label="Copy Braintrust trace URL"
                    onClick={() => {
                      void braintrustTraceUrl(traceId)
                        .then((url) => navigator.clipboard.writeText(url))
                        .then(
                          () => {
                            setTraceCopied(true);
                            window.setTimeout(() => setTraceCopied(false), 2000);
                          },
                          () => window.alert("Could not copy the Braintrust trace URL."),
                        );
                    }}
                  >
                    <svg
                      data-icon="inline-start"
                      viewBox="0 0 53 56"
                      aria-hidden="true"
                    >
                      <path
                        fill="currentColor"
                        d="M20.64 0c3.1 0 5.61 2.54 5.61 5.68v5.67a5.65 5.65 0 0 1-5.6 5.68h-2.81v2.2h2.8c3.1 0 5.61 2.55 5.61 5.68v5.68a5.64 5.64 0 0 1-5.6 5.68h-2.81v2.2h2.8c3.1 0 5.61 2.55 5.61 5.68v5.68a5.65 5.65 0 0 1-5.6 5.67h-5.62c-3.1 0-5.6-2.54-5.6-5.67v-3.94H6.61A5.65 5.65 0 0 1 1 40.2v-5.68a5.64 5.64 0 0 1 5.6-5.67h2.81v-2.21h-2.8A5.64 5.64 0 0 1 1 20.97V15.3a5.65 5.65 0 0 1 5.6-5.68h2.81V5.68A5.64 5.64 0 0 1 15.03 0zm19.33 0c3.1 0 5.6 2.54 5.6 5.68v3.94h2.81c3.1 0 5.61 2.54 5.61 5.68v5.67a5.64 5.64 0 0 1-5.6 5.68h-2.81v2.2h2.8c3.1 0 5.61 2.55 5.61 5.68v5.68a5.64 5.64 0 0 1-5.6 5.68h-2.81v3.94a5.64 5.64 0 0 1-5.61 5.67h-5.61c-3.1 0-5.61-2.54-5.61-5.67v-5.68a5.64 5.64 0 0 1 5.6-5.68h2.81v-2.2h-2.8c-3.1 0-5.61-2.55-5.61-5.68v-5.68a5.64 5.64 0 0 1 5.6-5.67h2.81v-2.21h-2.8c-3.1 0-5.61-2.54-5.61-5.68V5.68A5.64 5.64 0 0 1 34.35 0z"
                      />
                    </svg>
                  </InputGroupButton>
                }
              />
              <TooltipContent>
                {traceCopied ? "Copied Braintrust trace URL" : "Copy Braintrust trace URL"}
              </TooltipContent>
            </Tooltip>
          )}
          <span className="ml-auto" />
          {showMarkAsRead && (
            <InputGroupButton
              type="button"
              size="xs"
              disabled={isMarkingAsRead}
              onClick={onMarkAsRead}
            >
              {isMarkingAsRead ? "Marking as read…" : "Mark as read"}
            </InputGroupButton>
          )}
          {isBusy ? (
            <InputGroupButton
              type="button"
              size="icon-sm"
              variant="outline"
              aria-label="Stop"
              onClick={onStop}
            >
              <SquareIcon />
            </InputGroupButton>
          ) : (
            <InputGroupButton
              type="submit"
              size="icon-sm"
              variant="default"
              aria-label="Submit"
              disabled={!input.trim() || isChangingAgent}
            >
              <ArrowUpIcon />
            </InputGroupButton>
          )}
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
