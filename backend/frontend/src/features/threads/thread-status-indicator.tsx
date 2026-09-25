import type { Thread } from "@/lib/api-types";
import { cn } from "@/lib/utils";
import { threadActivity } from "./thread-activity";

const colors = [
  "bg-blue-500",
  "bg-violet-500",
  "bg-cyan-600",
  "bg-teal-600",
  "bg-amber-600",
  "bg-rose-500",
];

const rhythms = [
  "[--bot-blink-delay:-0.7s] [--bot-hop-delay:-0.2s]",
  "[--bot-blink-delay:-2.1s] [--bot-hop-delay:-0.8s]",
  "[--bot-blink-delay:-3.6s] [--bot-hop-delay:-1.4s]",
  "[--bot-blink-delay:-4.8s] [--bot-hop-delay:-1.1s]",
];

function identitySeed(id: string) {
  // Stable across polling, remounts, and changes to the thread's summary.
  let hash = 2166136261;
  for (let index = 0; index < id.length; index++) {
    hash = Math.imul(hash ^ id.charCodeAt(index), 16777619);
  }
  return hash >>> 0;
}

export function ThreadStatusIndicator({
  thread,
  forceAwake = false,
  size = "md",
}: {
  thread: Thread;
  forceAwake?: boolean;
  size?: "xs" | "sm" | "md";
}) {
  const { label, working, tone } = threadActivity(thread);
  const live =
    thread.live && !thread.archived && thread.activity?.phase !== "terminal";
  const settled = tone === "muted";
  const sandboxActive = forceAwake || (thread.activity?.sandbox_active ?? true);
  const awake = live && sandboxActive && !settled;
  const sleeping = live && !sandboxActive;
  const needsAttention = tone === "error";
  const paused = tone === "attention";
  const seed = identitySeed(thread.thread_id);
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex shrink-0 items-center justify-center",
        size === "xs"
          ? "size-4 rounded-[5px]"
          : size === "sm"
            ? "size-5 rounded-md"
            : "size-7 rounded-lg",
        live && !settled
          ? `${colors[seed % colors.length]} text-white`
          : "bg-zinc-200 text-zinc-500 dark:bg-zinc-700 dark:text-zinc-400",
        sleeping && "opacity-75 saturate-50",
        live &&
          tone === "error" &&
          "ring-2 ring-destructive/50 ring-offset-2 ring-offset-background",
        live &&
          paused &&
          "ring-2 ring-amber-400/70 ring-offset-2 ring-offset-background",
        rhythms[(seed >>> 8) % rhythms.length],
      )}
    >
      <span
        aria-hidden
        className={cn(
          "inline-flex origin-bottom",
          awake && working && "motion-safe:animate-bot-hop",
        )}
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={
            size === "xs"
              ? "size-3"
              : size === "sm"
                ? "size-3.5"
                : "size-[18px]"
          }
          focusable="false"
        >
          <path d="M12 8V4H8" />
          <rect x="4" y="8" width="16" height="12" rx="2" />
          <path d="M2 14h2m16 0h2" />
          <g
            className={cn(
              "origin-center [transform-box:fill-box]",
              awake && !needsAttention && "motion-safe:animate-bot-blink",
            )}
          >
            <path
              data-eye-state={
                needsAttention ? "attention" : sleeping ? "sleeping" : "awake"
              }
              d={
                needsAttention
                  ? "M8 13l2 2m0-2-2 2m6-2 2 2m0-2-2 2"
                  : sleeping
                    ? "M8 14c.6.7 1.4.7 2 0m4 0c.6.7 1.4.7 2 0"
                    : "M9 13v2m6-2v2"
              }
            />
          </g>
        </svg>
      </span>
    </span>
  );
}
