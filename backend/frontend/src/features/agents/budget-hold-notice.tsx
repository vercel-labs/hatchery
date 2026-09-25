import { CircleAlert } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { AgentBudget } from "@/lib/api-types";
import { number, reasonMessage } from "@/lib/format";

function resetLabel(seconds: number) {
  return new Date(seconds * 1_000).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

export function BudgetHoldNotice({
  agentId,
  budget,
  heldCount,
  compact = false,
  onGranted,
}: {
  agentId: string;
  budget: AgentBudget;
  heldCount: number;
  compact?: boolean;
  onGranted: () => Promise<unknown>;
}) {
  const [editing, setEditing] = useState(false);
  const [granting, setGranting] = useState(false);
  const [error, setError] = useState("");

  async function grant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (granting) return;
    const form = event.currentTarget;
    const amount = Number(new FormData(form).get("amount"));
    setGranting(true);
    setError("");
    try {
      await api(`/api/agents/${encodeURIComponent(agentId)}/grants`, {
        request_id: crypto.randomUUID(),
        amount,
      });
      setEditing(false);
      await onGranted().catch(() => undefined);
    } catch (reason) {
      setError(reasonMessage(reason));
    } finally {
      setGranting(false);
    }
  }

  const paused = `${number(heldCount)} thread${heldCount === 1 ? "" : "s"} paused`;
  return (
    <section
      aria-label="Daily budget exhausted"
      className={`rounded-xl border border-amber-400/50 bg-amber-400/10 text-amber-950 dark:text-amber-100 ${compact ? "p-3" : "mt-6 p-4"}`}
    >
      <div className="flex items-start gap-2.5">
        <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-xs font-semibold">Daily token budget exhausted</p>
          <p className="text-xs opacity-80">
            {heldCount ? `${paused}. ` : ""}Automatically resumes{" "}
            <time dateTime={new Date(budget.resets_at * 1_000).toISOString()}>
              {resetLabel(budget.resets_at)}
            </time>
            .
          </p>
        </div>
      </div>
      {editing ? (
        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(event) => void grant(event)}
        >
          <label className="min-w-28 flex-1 space-y-1 text-xs font-medium">
            Additional tokens
            <Input
              autoFocus
              disabled={granting}
              name="amount"
              type="number"
              min="1"
              max="1000000000"
              defaultValue={Math.max(1, Math.round(budget.limit / 10))}
              required
            />
          </label>
          <Button disabled={granting} size="sm" type="submit">
            {granting ? "Granting…" : "Grant"}
          </Button>
          <Button
            disabled={granting}
            size="sm"
            type="button"
            variant="ghost"
            onClick={() => setEditing(false)}
          >
            Cancel
          </Button>
        </form>
      ) : (
        <Button
          className="mt-2 text-amber-950 hover:bg-amber-400/20 dark:text-amber-100"
          size="xs"
          variant="ghost"
          onClick={() => setEditing(true)}
        >
          Grant tokens
        </Button>
      )}
      {error ? (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </section>
  );
}
