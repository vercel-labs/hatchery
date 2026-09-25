import { memo } from "react";
import { CornerDownRight } from "lucide-react";
import type { PendingPrompt } from "@/lib/api-types";

export const QueuedPrompts = memo(function QueuedPrompts({
  prompts,
}: {
  prompts: PendingPrompt[];
}) {
  if (!prompts.length) return null;
  return (
    <div
      role="region"
      aria-label="Queued messages"
      className="mb-2 max-h-40 space-y-2 overflow-y-auto overscroll-contain [scrollbar-width:thin]"
    >
      {prompts.map((prompt) => (
        <article
          key={prompt.requestId}
          className="flex min-w-0 items-start gap-3 rounded-xl border bg-muted/45 px-3.5 py-3 shadow-sm"
        >
          <CornerDownRight
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
          />
          <p className="min-w-0 flex-1 whitespace-pre-wrap wrap-anywhere text-sm leading-5">
            {prompt.text}
          </p>
          <span className="shrink-0 text-[11px] text-muted-foreground">
            Queued
          </span>
        </article>
      ))}
    </div>
  );
});
