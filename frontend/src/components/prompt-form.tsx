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
import { braintrustTraceUrl } from "@/lib/braintrust";

// Trimmed port of seal's prompt-form: text only, no attachments or model
// select.
export function PromptForm({
  isBusy,
  traceId,
  onSubmit,
  onStop,
}: {
  isBusy: boolean;
  traceId: string | null;
  onSubmit: (message: { text: string }) => void;
  onStop: () => void;
}) {
  const [input, setInput] = React.useState("");
  const [traceCopied, setTraceCopied] = React.useState(false);

  function handleSubmit(event?: React.FormEvent) {
    event?.preventDefault();
    const text = input.trim();
    if (!text || isBusy) return;
    onSubmit({ text });
    setInput("");
  }

  return (
    <form onSubmit={handleSubmit}>
      <InputGroup>
        <InputGroupTextarea
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
          {isBusy ? (
            <InputGroupButton
              type="button"
              size="icon-sm"
              variant="outline"
              aria-label="Stop"
              className="ml-auto"
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
              className="ml-auto"
              disabled={!input.trim()}
            >
              <ArrowUpIcon />
            </InputGroupButton>
          )}
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
