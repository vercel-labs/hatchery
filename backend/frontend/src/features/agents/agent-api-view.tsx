import { type FormEvent, type ReactNode, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useApi } from "@/hooks/use-api";
import { api } from "@/lib/api";
import type {
  AgentRoutes,
  AgentSchedules,
  AgentSecrets,
  RevealedAgentSecret,
} from "@/lib/api-types";
import { reasonMessage } from "@/lib/format";
import { Ellipsis, Eye, EyeOff, LoaderCircle } from "lucide-react";

type PendingSecretAction = {
  name: string;
  kind: "set" | "delete";
};

type PendingScheduleAction = {
  name: string;
  kind: "pause" | "resume";
};

// Ported from the agentmesh console against the serve endpoints
// (hatchery/serve/api.py). The GitHub connection stays on Hatchery's account menu.
export function AgentApiView({
  agentId,
  agentName,
  leading,
}: {
  agentId: string;
  agentName: string;
  leading?: ReactNode;
}) {
  const ownerPath = encodeURIComponent(agentId);
  const routes = useApi<AgentRoutes>(`/api/agents/${ownerPath}/routes`);
  const schedules = useApi<AgentSchedules>(
    `/api/agents/${ownerPath}/schedules`,
    2_000,
  );
  const inventory = useApi<AgentSecrets>(
    `/api/agents/${ownerPath}/secrets`,
  );
  const [revealed, setRevealed] = useState<Record<string, string>>({});
  const [pendingSecret, setPendingSecret] =
    useState<PendingSecretAction | null>(null);
  const [pendingReveal, setPendingReveal] = useState<string | null>(null);
  const [pendingSchedule, setPendingSchedule] =
    useState<PendingScheduleAction | null>(null);
  const [actionError, setActionError] = useState<{
    name: string;
    message: string;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [rotating, setRotating] = useState<string | null>(null);

  function hide(name: string) {
    setRevealed((current) => {
      const next = { ...current };
      delete next[name];
      return next;
    });
  }

  async function setSecret(event: FormEvent<HTMLFormElement>, name: string) {
    event.preventDefault();
    if (pendingSecret) return;
    const form = event.currentTarget;
    const value = String(new FormData(form).get("value") ?? "");
    setPendingSecret({ name, kind: "set" });
    setActionError(null);
    try {
      await api(
        `/api/agents/${ownerPath}/secrets/${encodeURIComponent(name)}`,
        {
          request_id: crypto.randomUUID(),
          value,
        },
      );
      form.reset();
      hide(name);
      setRotating(null);
      await inventory.mutate().catch(() => undefined);
    } catch (reason) {
      setActionError({ name, message: reasonMessage(reason) });
    } finally {
      setPendingSecret(null);
    }
  }

  async function reveal(name: string) {
    if (pendingReveal) return;
    setPendingReveal(name);
    setActionError(null);
    try {
      const result = await api<RevealedAgentSecret>(
        `/api/agents/${ownerPath}/secrets/${encodeURIComponent(name)}/reveal`,
        { request_id: crypto.randomUUID() },
      );
      setRevealed((current) => ({ ...current, [name]: result.value }));
    } catch (reason) {
      setActionError({ name, message: reasonMessage(reason) });
    } finally {
      setPendingReveal(null);
    }
  }

  async function deleteSecret(name: string) {
    if (pendingSecret) return;
    setPendingSecret({ name, kind: "delete" });
    setActionError(null);
    try {
      await api(
        `/api/agents/${ownerPath}/secrets/${encodeURIComponent(name)}/delete`,
        { request_id: crypto.randomUUID() },
      );
      hide(name);
      setRotating(null);
      setConfirmDelete(null);
      await inventory.mutate().catch(() => undefined);
    } catch (reason) {
      setActionError({ name, message: reasonMessage(reason) });
    } finally {
      setPendingSecret(null);
    }
  }

  async function setSchedulePaused(name: string, paused: boolean) {
    if (pendingSchedule) return;
    setPendingSchedule({ name, kind: paused ? "pause" : "resume" });
    setActionError(null);
    try {
      await api(
        `/api/agents/${ownerPath}/schedules/${encodeURIComponent(name)}/${paused ? "pause" : "resume"}`,
        { request_id: crypto.randomUUID() },
      );
      await schedules.mutate(
        (current) =>
          current
            ? {
                ...current,
                schedules: current.schedules.map((schedule) =>
                  schedule.name === name
                    ? {
                        ...schedule,
                        paused,
                        next_at: paused ? null : schedule.next_at,
                      }
                    : schedule,
                ),
              }
            : current,
        { revalidate: false },
      );
    } catch (reason) {
      setActionError({ name, message: reasonMessage(reason) });
    } finally {
      setPendingSchedule(null);
    }
  }

  return (
    <>
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-4">
        {leading}
        <h2 className="truncate text-sm font-medium">{agentName} / API</h2>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="mx-auto max-w-5xl space-y-10 px-5 py-8 md:px-8">

          <section aria-labelledby="agent-routes-heading">
            <div className="mb-4 space-y-1">
              <h3 id="agent-routes-heading" className="text-sm font-medium">
                Routes
              </h3>
              {routes.data ? (
                <p className="text-xs text-muted-foreground">
                  {routes.data.routes.length} public{" "}
                  {routes.data.routes.length === 1 ? "route" : "routes"} from
                  revision{" "}
                  <code title={routes.data.revision}>
                    {routes.data.revision.slice(0, 12)}
                  </code>
                </p>
              ) : null}
            </div>
            {routes.error ? (
              <p role="alert" className="mb-4 text-sm text-destructive">
                {routes.error.message}
              </p>
            ) : null}
            {!routes.data && !routes.error ? (
              <p role="status" className="text-sm text-muted-foreground">
                Loading routes...
              </p>
            ) : routes.data?.routes.length ? (
              <ul className="divide-y border-y">
                {routes.data.routes.map((route) => (
                  <li
                    key={`${route.path}:${route.methods.join(",")}`}
                    className="py-4"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      {route.methods.map((method) => (
                        <Badge
                          key={method}
                          variant="outline"
                          className="font-mono"
                        >
                          {method}
                        </Badge>
                      ))}
                      <code className="break-all text-sm font-medium">
                        {route.path}
                      </code>
                      <span
                        className={`ml-auto text-xs ${route.available ? "text-muted-foreground" : "text-destructive"}`}
                      >
                        {route.available ? "Available" : "Unavailable"}
                      </span>
                    </div>
                    {route.description ? (
                      <p className="mt-2 text-sm text-muted-foreground">
                        {route.description}
                      </p>
                    ) : null}
                    <p className="mt-2 break-all text-xs">
                      {route.available ? (
                        <a
                          className="text-muted-foreground underline underline-offset-2 hover:text-foreground"
                          href={route.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {route.url}
                        </a>
                      ) : (
                        <span className="text-muted-foreground">
                          {route.url}
                        </span>
                      )}
                    </p>
                    {route.error ? (
                      <p className="mt-2 text-xs text-destructive">
                        {route.error}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : routes.data ? (
              <p className="border-y py-4 text-sm text-muted-foreground">
                This agent does not expose any routes.
              </p>
            ) : null}
          </section>

          <section aria-labelledby="agent-schedules-heading">
            <div className="mb-4 space-y-1">
              <h3 id="agent-schedules-heading" className="text-sm font-medium">
                Schedules
              </h3>
              {schedules.data ? (
                <p className="text-xs text-muted-foreground">
                  {schedules.data.schedules.length} workspace{" "}
                  {schedules.data.schedules.length === 1 ? "job" : "jobs"} from
                  revision{" "}
                  <code title={schedules.data.revision}>
                    {schedules.data.revision.slice(0, 12)}
                  </code>
                  {schedules.data.reconciled_revision !==
                  schedules.data.revision
                    ? " (reconciling)"
                    : ""}
                </p>
              ) : null}
            </div>
            {schedules.error ? (
              <p role="alert" className="mb-4 text-sm text-destructive">
                {schedules.error.message}
              </p>
            ) : null}
            {!schedules.data && !schedules.error ? (
              <p role="status" className="text-sm text-muted-foreground">
                Loading schedules...
              </p>
            ) : schedules.data?.schedules.length ? (
              <ul className="divide-y border-y">
                {schedules.data.schedules.map((schedule) => (
                  <li key={schedule.name} className="py-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="text-sm font-medium">
                        {schedule.name}
                      </code>
                      {schedule.kind && schedule.value ? (
                        <Badge
                          variant="outline"
                          className={`font-mono ${schedule.paused ? "border-muted-foreground/20 text-muted-foreground/55" : ""}`}
                        >
                          {schedule.kind === "cron"
                            ? schedule.value
                            : `every ${schedule.value}`}
                        </Badge>
                      ) : null}
                      {schedule.timezone ? (
                        <Badge variant="secondary">{schedule.timezone}</Badge>
                      ) : null}
                      <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                        <span
                          aria-hidden
                          className={`size-1.5 rounded-full ${!schedule.enabled || !schedule.available ? "bg-destructive" : schedule.paused ? "bg-amber-500" : schedule.running ? "bg-blue-500" : "bg-emerald-500"}`}
                        />
                        {!schedule.enabled
                          ? "Disabled"
                          : schedule.paused
                            ? "Paused"
                            : schedule.running
                              ? "Running"
                              : schedule.available
                                ? "Scheduled"
                                : "Unavailable"}
                      </span>
                      {schedule.available && schedule.enabled ? (
                        <Button
                          size="xs"
                          variant="outline"
                          className="w-20"
                          disabled={Boolean(pendingSchedule)}
                          aria-label={
                            pendingSchedule?.name === schedule.name
                              ? schedule.paused
                                ? "Resuming..."
                                : "Pausing..."
                              : schedule.paused
                                ? "Resume"
                                : "Pause"
                          }
                          onClick={() =>
                            void setSchedulePaused(
                              schedule.name,
                              !schedule.paused,
                            )
                          }
                        >
                          {pendingSchedule?.name === schedule.name &&
                          pendingSchedule.kind ===
                            (schedule.paused ? "resume" : "pause") ? (
                            <LoaderCircle
                              className="animate-spin"
                              aria-hidden
                            />
                          ) : schedule.paused ? (
                            "Resume"
                          ) : (
                            "Pause"
                          )}
                        </Button>
                      ) : null}
                    </div>
                    {schedule.description ? (
                      <p className="mt-2 text-sm text-muted-foreground">
                        {schedule.description}
                      </p>
                    ) : null}
                    {schedule.next_at !== null ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        Next run{" "}
                        <time
                          dateTime={new Date(
                            schedule.next_at * 1_000,
                          ).toISOString()}
                        >
                          {new Date(schedule.next_at * 1_000).toLocaleString()}
                        </time>
                      </p>
                    ) : null}
                    {schedule.last_run ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        Last run returned {schedule.last_run.status} at{" "}
                        <time
                          dateTime={new Date(
                            schedule.last_run.finished_at * 1_000,
                          ).toISOString()}
                        >
                          {new Date(
                            schedule.last_run.finished_at * 1_000,
                          ).toLocaleString()}
                        </time>
                      </p>
                    ) : null}
                    {schedule.error || schedule.last_run?.error ? (
                      <p className="mt-2 text-xs text-destructive">
                        {schedule.error || schedule.last_run?.error}
                      </p>
                    ) : null}
                    {actionError?.name === schedule.name ? (
                      <p role="alert" className="mt-2 text-xs text-destructive">
                        {actionError.message}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : schedules.data ? (
              <p className="border-y py-4 text-sm text-muted-foreground">
                This agent does not run any workspace schedules.
              </p>
            ) : null}
          </section>

          <section aria-labelledby="agent-secrets-heading">
            <div className="mb-4 space-y-1">
              <h3 id="agent-secrets-heading" className="text-sm font-medium">
                Secrets
              </h3>
              <p className="text-xs text-muted-foreground">
                Values are encrypted at rest and revealed only on request.
              </p>
            </div>
            {inventory.error ? (
              <p role="alert" className="mb-4 text-sm text-destructive">
                {inventory.error.message}
              </p>
            ) : null}
            {!inventory.data && !inventory.error ? (
              <p role="status" className="text-sm text-muted-foreground">
                Loading secrets...
              </p>
            ) : inventory.data?.secrets.length ? (
              <ul className="space-y-2">
                {inventory.data.secrets.map((secret) => {
                  const busy = pendingSecret?.name === secret.name;
                  const editing = !secret.stored || rotating === secret.name;
                  return (
                    <li
                      key={secret.name}
                      className="rounded-lg border px-4 py-4"
                    >
                      <div className="flex flex-wrap items-center gap-4">
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <code className="text-sm font-medium">
                              {secret.name}
                            </code>
                            <Badge
                              variant={secret.stored ? "secondary" : "outline"}
                            >
                              {secret.stored ? "Stored" : "Not set"}
                            </Badge>
                          </div>
                          {secret.note ? (
                            <p className="text-xs text-muted-foreground">
                              {secret.note}
                            </p>
                          ) : null}
                          {secret.updated_at !== null ? (
                            <p className="text-xs text-muted-foreground">
                              Updated{" "}
                              <time
                                dateTime={new Date(
                                  secret.updated_at * 1_000,
                                ).toISOString()}
                              >
                                {new Date(
                                  secret.updated_at * 1_000,
                                ).toLocaleString()}
                              </time>
                            </p>
                          ) : null}
                        </div>
                        {secret.stored ? (
                          <div className="flex min-w-48 flex-1 items-center gap-2 md:max-w-md">
                            <Button
                              size="icon-sm"
                              variant="ghost"
                              disabled={pendingReveal !== null}
                              aria-label={
                                pendingReveal === secret.name
                                  ? `Revealing ${secret.name}`
                                  : `${revealed[secret.name] ? "Hide" : "Reveal"} ${secret.name}`
                              }
                              onClick={() =>
                                revealed[secret.name]
                                  ? hide(secret.name)
                                  : void reveal(secret.name)
                              }
                            >
                              {pendingReveal === secret.name ? (
                                <LoaderCircle
                                  className="animate-spin"
                                  aria-hidden
                                />
                              ) : revealed[secret.name] ? (
                                <EyeOff aria-hidden />
                              ) : (
                                <Eye aria-hidden />
                              )}
                            </Button>
                            <code className="min-w-0 flex-1 truncate text-sm text-muted-foreground">
                              {revealed[secret.name] || "••••••••••••"}
                            </code>
                          </div>
                        ) : null}
                        {secret.stored ? (
                          <div className="ml-auto">
                            <DropdownMenu>
                              <DropdownMenuTrigger
                                aria-label={`Actions for ${secret.name}`}
                                disabled={Boolean(pendingSecret)}
                                className={buttonVariants({
                                  variant: "outline",
                                  size: "icon-sm",
                                })}
                              >
                                <Ellipsis className="size-4" aria-hidden />
                              </DropdownMenuTrigger>
                              <DropdownMenuContent
                                align="end"
                                sideOffset={6}
                                className="min-w-36"
                              >
                                <DropdownMenuItem
                                  onClick={() => {
                                    setRotating(secret.name);
                                    setConfirmDelete(null);
                                  }}
                                >
                                  Rotate
                                </DropdownMenuItem>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem
                                  variant="destructive"
                                  onClick={() => {
                                    setConfirmDelete(secret.name);
                                    setRotating(null);
                                  }}
                                >
                                  Delete
                                </DropdownMenuItem>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </div>
                        ) : null}
                      </div>
                      {confirmDelete === secret.name ? (
                        <div
                          className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/20 bg-destructive/5 px-3 py-2"
                          role="group"
                          aria-label={`Confirm delete ${secret.name}`}
                        >
                          <span className="text-xs text-destructive">
                            Permanently delete {secret.name}?
                          </span>
                          <div className="flex items-center gap-2">
                            <Button
                              size="xs"
                              variant="outline"
                              disabled={Boolean(pendingSecret)}
                              onClick={() => setConfirmDelete(null)}
                            >
                              Cancel
                            </Button>
                            <Button
                              size="xs"
                              variant="destructive"
                              disabled={Boolean(pendingSecret)}
                              onClick={() => void deleteSecret(secret.name)}
                            >
                              {busy && pendingSecret?.kind === "delete"
                                ? "Deleting..."
                                : "Delete"}
                            </Button>
                          </div>
                        </div>
                      ) : null}
                      {editing ? (
                        <form
                          className="mt-3 flex w-full flex-wrap items-center gap-2 rounded-md bg-muted/35 p-3"
                          onSubmit={(event) =>
                            void setSecret(event, secret.name)
                          }
                        >
                          <label
                            className="sr-only"
                            htmlFor={`secret-${secret.name}`}
                          >
                            {secret.stored ? "New value" : "Value"} for{" "}
                            {secret.name}
                          </label>
                          <Input
                            id={`secret-${secret.name}`}
                            name="value"
                            type="password"
                            autoComplete="new-password"
                            required
                            disabled={Boolean(pendingSecret)}
                            placeholder={
                              secret.stored ? "New value" : "Secret value"
                            }
                            className="min-w-52 flex-1 bg-background"
                          />
                          <div className="ml-auto flex items-center gap-2">
                            {secret.stored ? (
                              <Button
                                type="button"
                                size="xs"
                                variant="outline"
                                disabled={Boolean(pendingSecret)}
                                onClick={() => setRotating(null)}
                              >
                                Cancel
                              </Button>
                            ) : null}
                            <Button
                              type="submit"
                              size="xs"
                              disabled={Boolean(pendingSecret)}
                              aria-label={`${secret.stored ? "Rotate" : "Set"} ${secret.name}`}
                            >
                              {busy && pendingSecret?.kind === "set"
                                ? "Saving..."
                                : secret.stored
                                  ? "Rotate"
                                  : "Set"}
                            </Button>
                          </div>
                        </form>
                      ) : null}
                      {actionError?.name === secret.name ? (
                        <p
                          role="alert"
                          className="mt-2 text-xs text-destructive"
                        >
                          {actionError.message}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            ) : inventory.data ? (
              <p className="border-y py-4 text-sm text-muted-foreground">
                This agent has no requested or stored secrets.
              </p>
            ) : null}
          </section>
        </div>
      </div>
    </>
  );
}
