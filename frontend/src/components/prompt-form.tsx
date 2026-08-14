import { ArrowUpIcon, SquareIcon } from "lucide-react";
import * as React from "react";

import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";

// Trimmed port of seal's prompt-form: text only, no attachments or model
// select.
export function PromptForm({
  isBusy,
  onSubmit,
  onStop,
}: {
  isBusy: boolean;
  onSubmit: (message: { text: string }) => void;
  onStop: () => void;
}) {
  const [input, setInput] = React.useState("");

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
