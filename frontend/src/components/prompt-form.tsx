import { ArrowUpIcon, ListTreeIcon, SquareIcon } from "lucide-react";
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
import { AUTO_SPACE_VALUE } from "@/components/new-chat-state";
import { tracesUrl } from "@/lib/traces";
import type { Space } from "@/lib/api";
import { resolveSpaceColor } from "@/lib/space-colors";

// Trimmed port of seal's prompt-form: text only, no attachments or model
// select.
export function PromptForm({
  isBusy,
  traceId,
  spaces,
  spaceId,
  showMarkAsRead,
  isMarkingAsRead,
  showAutoSpace = false,
  autoFocus = false,
  onSubmit,
  onStop,
  onSpaceChange,
  onMarkAsRead,
}: {
  isBusy: boolean;
  traceId: string | null;
  spaces: Space[];
  spaceId: string | null;
  showMarkAsRead: boolean;
  isMarkingAsRead: boolean;
  showAutoSpace?: boolean;
  autoFocus?: boolean;
  onSubmit: (message: { text: string }) => void | Promise<void>;
  onStop: () => void;
  onSpaceChange: (spaceId: string) => void | Promise<void>;
  onMarkAsRead: () => void;
}) {
  const [input, setInput] = React.useState("");
  const [traceCopied, setTraceCopied] = React.useState(false);
  const [isChangingSpace, setIsChangingSpace] = React.useState(false);
  const selectedSpace = spaces.find((space) => space.id === spaceId);

  async function handleSubmit(event?: React.FormEvent) {
    event?.preventDefault();
    const text = input.trim();
    if (!text || isBusy || isChangingSpace) return;
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
          {(spaceId || showAutoSpace) && (
            <Select
              disabled={isChangingSpace}
              value={spaceId ?? AUTO_SPACE_VALUE}
              onValueChange={(nextSpaceId) => {
                if (nextSpaceId && nextSpaceId !== AUTO_SPACE_VALUE) {
                  setIsChangingSpace(true);
                  Promise.resolve(onSpaceChange(nextSpaceId)).finally(() =>
                    setIsChangingSpace(false),
                  );
                }
              }}
            >
              <SelectTrigger
                size="sm"
                aria-label="Chat space"
                className="h-6 max-w-44 rounded-xl border-transparent bg-secondary px-2 text-secondary-foreground hover:bg-secondary/80"
              >
                <SelectValue>
                  {selectedSpace ? (
                    <>
                      <span
                        className="size-2 shrink-0 rounded-full"
                        style={{
                          backgroundColor: resolveSpaceColor(selectedSpace.color),
                        }}
                      />
                      <span className="truncate">{selectedSpace.name}</span>
                    </>
                  ) : (
                    <span className="truncate">Auto</span>
                  )}
                </SelectValue>
              </SelectTrigger>
              <SelectContent align="start">
                <SelectGroup>
                  {showAutoSpace && (
                    <SelectItem value={AUTO_SPACE_VALUE}>Auto</SelectItem>
                  )}
                  {spaces.map((space) => (
                    <SelectItem key={space.id} value={space.id}>
                      <span
                        className="size-2 shrink-0 rounded-full"
                        style={{ backgroundColor: resolveSpaceColor(space.color) }}
                      />
                      {space.name}
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
                    aria-label="Copy trace URL"
                    onClick={() => {
                      void tracesUrl(traceId)
                        .then((url) => navigator.clipboard.writeText(url))
                        .then(
                          () => {
                            setTraceCopied(true);
                            window.setTimeout(() => setTraceCopied(false), 2000);
                          },
                          () => window.alert("Could not copy the trace URL."),
                        );
                    }}
                  >
                    <ListTreeIcon data-icon="inline-start" aria-hidden="true" />
                  </InputGroupButton>
                }
              />
              <TooltipContent>
                {traceCopied ? "Copied trace URL" : "Copy trace URL"}
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
              disabled={!input.trim() || isChangingSpace}
            >
              <ArrowUpIcon />
            </InputGroupButton>
          )}
        </InputGroupAddon>
      </InputGroup>
    </form>
  );
}
