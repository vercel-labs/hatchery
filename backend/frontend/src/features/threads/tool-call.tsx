import { useState, type FormEvent } from "react";
import {
  CalendarClock,
  CalendarX2,
  CheckCircle2,
  ClipboardCheck,
  GitBranch,
  GitFork,
  KeyRound,
  Send,
  SquareTerminal,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { reasonMessage } from "@/lib/format";
import type { Part, ToolItem } from "./transcript-model";

const disclosureIcon = (
  <svg
    aria-hidden
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    className="absolute inset-0 size-4 opacity-0 transition group-hover/tool:opacity-100 group-open/tool:rotate-90"
  >
    <path d="m9 5 7 7-7 7" />
  </svg>
);

type ToolPresentation = {
  completed: string;
  failed: string;
  icon: LucideIcon;
  input: string;
  running: string;
  shell: boolean;
};

function describeCommand(command: string, toolName: string): string {
  const line = command
    .split("\n")
    .find((candidate) => candidate.trim())
    ?.trim();
  if (!line) return `View ${toolName} result`;
  if (/^echo(?:\s|$)/.test(line)) {
    const value = line.replace(/^echo\s*/, "").replace(/^(['"])(.*)\1$/, "$2");
    return value ? `Echo ${value} in Bash` : "Print a blank line in Bash";
  }
  if (/^cat(?:\s|$)/.test(line))
    return `Read ${line.replace(/^cat\s*/, "") || "a file"}`;
  if (/^ls(?:\s|$)/.test(line)) return "List files";
  if (line === "pwd") return "Show the working directory";
  if (/^git\s+status(?:\s|$)/.test(line)) return "Check Git status";
  if (/^git\s+diff(?:\s|$)/.test(line)) return "Review Git changes";
  if (/^(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:test|vitest)(?:\s|$)/.test(line))
    return "Run the tests";
  if (/^(?:npm|pnpm|yarn)\s+(?:run\s+)?build(?:\s|$)/.test(line))
    return "Build the project";
  const executable = line.match(/^(?:\S+=\S+\s+)*(\S+)/)?.[1];
  return executable
    ? `Run ${executable.replace(/^.*\//, "")} in Bash`
    : "Run a Bash command";
}

function parseArguments(value: unknown): Record<string, unknown> | null {
  let parsed = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      return null;
    }
  }
  return parsed && typeof parsed === "object"
    ? (parsed as Record<string, unknown>)
    : null;
}

function serializedArguments(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value ?? {}, null, 2);
}

function argument(args: Record<string, unknown> | null, name: string): string {
  const value = args?.[name];
  return typeof value === "string" ? value.trim() : "";
}

function displayName(value: string): string {
  return value
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function bashPresentation(value: unknown, toolName: string): ToolPresentation {
  const args = parseArguments(value);
  const command = argument(args, "command") || serializedArguments(value);
  const suppliedDescription = argument(args, "description");
  const description = suppliedDescription || describeCommand(command, toolName);
  return {
    completed: description,
    failed: description,
    icon: SquareTerminal,
    input: command,
    running: description,
    shell: true,
  };
}

function toolPresentation(value: unknown, toolName: string): ToolPresentation {
  if (toolName === "bash") return bashPresentation(value, toolName);

  const args = parseArguments(value);
  const input = serializedArguments(value);
  if (toolName === "skill_view") {
    const name = argument(args, "name");
    const skill = name ? `${displayName(name)} skill` : "skill";
    const file = argument(args, "file_path");
    const target = file ? `${file} from the ${skill}` : skill;
    return {
      completed: `Read ${target}`,
      failed: `Failed to read ${target}`,
      icon: Wrench,
      input,
      running: `Reading ${target}`,
      shell: false,
    };
  }
  if (toolName === "signal" || toolName === "schedule") {
    const delay = argument(args, "delay");
    const timing = delay ? ` for ${delay}` : "";
    return {
      completed: `Scheduled signal${timing}`,
      failed: "Failed to schedule signal",
      icon: CalendarClock,
      input,
      running: `Scheduling signal${timing}`,
      shell: false,
    };
  }
  if (toolName === "cancel") {
    return {
      completed: "Cancelled signal",
      failed: "Failed to cancel signal",
      icon: CalendarX2,
      input,
      running: "Cancelling signal",
      shell: false,
    };
  }
  if (toolName === "open_repository") {
    const repository = argument(args, "repository");
    const target = repository ? ` ${repository}` : " repository";
    return {
      completed: `Opened${target}`,
      failed: `Failed to open${target}`,
      icon: GitBranch,
      input,
      running: `Opening${target}`,
      shell: false,
    };
  }
  if (toolName === "delegate") {
    return {
      completed: "Delegated task",
      failed: "Failed to delegate task",
      icon: GitFork,
      input,
      running: "Delegating task",
      shell: false,
    };
  }
  if (toolName === "message_parent") {
    return {
      completed: "Messaged parent",
      failed: "Failed to message parent",
      icon: Send,
      input,
      running: "Messaging parent",
      shell: false,
    };
  }
  if (toolName === "complete") {
    return {
      completed: "Completed delegated task",
      failed: "Failed to complete delegated task",
      icon: ClipboardCheck,
      input,
      running: "Completing delegated task",
      shell: false,
    };
  }

  const name = displayName(toolName) || "tool";
  return {
    completed: `Ran ${name}`,
    failed: `Failed to run ${name}`,
    icon: Wrench,
    input,
    running: `Running ${name}`,
    shell: false,
  };
}

function toolOutput(result: unknown, execution: Part | null): string {
  if (execution && typeof execution.stdout === "string")
    return execution.stdout;
  if (typeof result === "string") return result;
  return JSON.stringify(result ?? null, null, 2);
}

function SecretRequestCard({
  item,
  owner,
  failed,
}: {
  item: ToolItem;
  owner?: string;
  failed: boolean;
}) {
  const args = parseArguments(item.call?.tool_args);
  const name = argument(args, "name") || "Secret";
  const note = argument(args, "note");
  const ready = Boolean(item.result);
  const failureReason =
    failed && typeof item.result?.result === "string"
      ? item.result.result
      : "The request was not recorded.";
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  async function setSecret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!owner || saving) return;
    const form = event.currentTarget;
    const value = String(new FormData(form).get("value") ?? "");
    setSaving(true);
    setError("");
    try {
      await api(
        `/api/agents/${encodeURIComponent(owner)}/secrets/${encodeURIComponent(name)}`,
        { request_id: crypto.randomUUID(), value },
      );
      form.reset();
      setSaved(true);
    } catch (reason) {
      setError(reasonMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section
      aria-label={`Secret request for ${name}`}
      className={`min-w-0 overflow-hidden rounded-lg border px-3.5 py-3 text-sm ${
        failed
          ? "border-destructive/30 bg-destructive/5"
          : "border-amber-500/25 bg-amber-500/5"
      }`}
    >
      <div className="flex items-start gap-3">
        <KeyRound
          aria-hidden
          className={`mt-0.5 size-4 shrink-0 ${
            failed ? "text-destructive" : "text-amber-600 dark:text-amber-400"
          }`}
          strokeWidth={1.75}
        />
        <div className="min-w-0 flex-1 space-y-2.5">
          <div className="space-y-1">
            <p className="font-medium">
              {failed
                ? "Secret request failed"
                : ready
                  ? "Secret requested"
                  : "Requesting secret"}
            </p>
            <code className="block break-all text-xs font-medium">{name}</code>
            {note ? (
              <p className="text-xs leading-5 text-muted-foreground">{note}</p>
            ) : null}
          </div>
          {failed ? (
            <p className="text-xs text-destructive">{failureReason}</p>
          ) : saved ? (
            <p
              role="status"
              className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400"
            >
              <CheckCircle2 aria-hidden className="size-3.5" />
              Secret saved for this agent.
            </p>
          ) : !ready ? (
            <p role="status" className="text-xs text-muted-foreground">
              Recording the request...
            </p>
          ) : owner ? (
            <form
              className="space-y-2"
              onSubmit={(event) => void setSecret(event)}
            >
              <div className="flex max-w-xl items-center gap-2">
                <label
                  className="sr-only"
                  htmlFor={`secret-request-${item.key}`}
                >
                  Value for {name}
                </label>
                <Input
                  id={`secret-request-${item.key}`}
                  name="value"
                  type="password"
                  autoComplete="new-password"
                  required
                  disabled={saving}
                  placeholder="Secret value"
                />
                <Button type="submit" size="sm" disabled={saving}>
                  {saving ? "Saving..." : "Approve & save"}
                </Button>
              </div>
              <p className="text-[11px] leading-4 text-muted-foreground">
                Saving approves this request. The value is encrypted and never
                added to the transcript.
              </p>
              {error ? (
                <p role="alert" className="text-xs text-destructive">
                  {error}
                </p>
              ) : null}
            </form>
          ) : (
            <p className="text-xs text-muted-foreground">
              Open this agent&apos;s API settings to provide the value.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

export function ToolCard({
  item,
  owner,
}: {
  item: ToolItem;
  owner?: string;
}) {
  const presentation = toolPresentation(item.call?.tool_args, item.name);
  const result = item.result?.result;
  const execution =
    result && typeof result === "object" ? (result as Part) : null;
  const exitCode =
    typeof execution?.exit_code === "number" ? execution.exit_code : undefined;
  const timedOut = presentation.shell && exitCode === 124;
  const failed =
    item.result?.result_kind === "error" ||
    (exitCode !== undefined && exitCode !== 0 && !timedOut);
  if (item.name === "secret_request") {
    return (
      <SecretRequestCard
        item={item}
        owner={owner}
        failed={failed}
      />
    );
  }
  const status = !item.result
    ? item.execution === "running"
      ? "Running"
      : item.execution === "queued"
        ? "Queued"
        : item.execution === "stopped"
          ? "Stopped"
          : "Awaiting result"
    : timedOut
      ? "Timed out"
      : failed
        ? "Failed"
        : "Completed";
  const output = presentation.shell
    ? toolOutput(result, execution)
    : typeof result === "string"
      ? result
      : JSON.stringify(result ?? null, null, 2);
  const stderr =
    presentation.shell && typeof execution?.stderr === "string"
      ? execution.stderr
      : "";

  if (!presentation.shell) {
    const Icon = presentation.icon;
    const description = failed
      ? presentation.failed
      : item.result
        ? presentation.completed
        : presentation.running;
    const active = item.execution === "running" && !item.result;
    return (
      <details
        className="group/tool min-w-0 text-xs text-muted-foreground"
        aria-label={`${item.name} tool call`}
      >
        <summary className="flex w-fit max-w-full cursor-pointer list-none items-center gap-2 rounded-sm py-0.5 hover:text-foreground focus-visible:text-foreground focus-visible:underline focus-visible:underline-offset-4 focus-visible:outline-none [&::-webkit-details-marker]:hidden">
          <Icon aria-hidden className="size-4 shrink-0" strokeWidth={1.75} />
          <span
            className={`truncate ${active ? "thinking-shimmer font-medium" : ""} ${failed ? "text-destructive" : ""}`}
            title={description}
          >
            {description}
          </span>
          <span className="sr-only">{status}</span>
        </summary>
        <div className="ml-2 mt-2 space-y-2.5 border-l border-border/70 pl-6 font-mono leading-5">
          {item.call ? (
            <pre
              aria-label="Tool input"
              className="max-h-40 overflow-auto whitespace-pre-wrap wrap-anywhere"
            >
              {presentation.input}
            </pre>
          ) : null}
          {item.result ? (
            <div className="space-y-2.5">
              {output ? (
                <pre
                  aria-label="Tool output"
                  className="max-h-80 overflow-auto whitespace-pre-wrap wrap-anywhere"
                >
                  {output}
                </pre>
              ) : stderr ? null : (
                <p className="font-sans">Tool completed with no output.</p>
              )}
              {stderr ? (
                <pre
                  aria-label="Standard error"
                  className="max-h-60 overflow-auto whitespace-pre-wrap wrap-anywhere text-destructive"
                >
                  {stderr}
                </pre>
              ) : null}
            </div>
          ) : (
            <p className="font-sans">
              {item.execution === "queued"
                ? "Waiting for the current tool to finish."
                : item.execution === "stopped"
                  ? "The thread stopped before a result was recorded."
                  : "The result will appear here when available."}
            </p>
          )}
        </div>
      </details>
    );
  }

  const TerminalIcon = presentation.icon;
  return (
    <details
      className="group/tool min-w-0 overflow-hidden rounded-md border border-border/70 bg-muted/10 text-xs"
      aria-label={`${item.name} tool call`}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md px-2.5 py-2 hover:bg-muted/50 focus-visible:bg-muted/70 focus-visible:outline-none [&::-webkit-details-marker]:hidden">
        <span className="relative size-4 shrink-0 text-muted-foreground">
          <TerminalIcon
            aria-hidden
            className="absolute inset-0 size-4 transition-opacity group-hover/tool:opacity-0"
            strokeWidth={1.75}
          />
          {disclosureIcon}
        </span>
        <span
          className="min-w-0 flex-1 truncate"
          title={presentation.completed}
        >
          {presentation.completed}
        </span>
        {status === "Completed" ? (
          <span className="sr-only">Completed</span>
        ) : (
          <span
            className={`ml-auto flex shrink-0 items-center gap-1.5 text-[11px] ${
              timedOut
                ? "text-amber-600 dark:text-amber-400"
                : failed
                  ? "text-destructive"
                  : "text-muted-foreground"
            }`}
          >
            <span
              aria-hidden
              className={
                item.execution === "running" && !item.result
                  ? "size-3 animate-spin rounded-full border-2 border-blue-500/25 border-t-blue-500 motion-reduce:animate-none"
                  : `size-1.5 rounded-full ${timedOut ? "bg-amber-500" : failed ? "bg-destructive" : item.result ? "bg-emerald-500" : item.execution === "stopped" ? "bg-muted-foreground" : "bg-amber-500"}`
              }
            />
            <span>{status}</span>
            {exitCode !== undefined && exitCode !== 0 ? (
              <span>· exit {exitCode}</span>
            ) : null}
          </span>
        )}
      </summary>
      <div className="space-y-2.5 border-t border-border/70 px-2.5 py-2.5 font-mono leading-5">
        {item.call ? (
          <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-2">
            <span aria-hidden className="select-none text-muted-foreground">
              $
            </span>
            <pre
              aria-label="Command"
              className="max-h-40 overflow-auto whitespace-pre-wrap wrap-anywhere"
            >
              {presentation.input}
            </pre>
          </div>
        ) : null}
        {item.result ? (
          <div className="space-y-2.5">
            {output ? (
              <pre
                aria-label="Tool output"
                className="max-h-80 overflow-auto whitespace-pre-wrap wrap-anywhere text-muted-foreground"
              >
                {output}
              </pre>
            ) : stderr ? null : (
              <p className="text-muted-foreground">
                Command completed with no output.
              </p>
            )}
            {stderr ? (
              <pre
                aria-label="Standard error"
                className={`max-h-60 overflow-auto whitespace-pre-wrap wrap-anywhere ${
                  timedOut
                    ? "text-amber-600 dark:text-amber-400"
                    : "text-destructive"
                }`}
              >
                {stderr}
              </pre>
            ) : null}
          </div>
        ) : (
          <p className="font-sans text-muted-foreground">
            {item.execution === "queued"
              ? "Waiting for the current command to finish. Bash calls in this thread run one at a time."
              : item.execution === "running"
                ? "This command is running. Its result will appear when it finishes."
                : item.execution === "stopped"
                  ? "The thread stopped before a result was recorded."
                  : "The result will appear here when available."}
          </p>
        )}
      </div>
    </details>
  );
}
