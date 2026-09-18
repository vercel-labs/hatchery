import { useCallback, useEffect, useState } from "react";
import { Clock3Icon, PauseIcon, PlayIcon, RefreshCwIcon } from "lucide-react";

import { apiFetch } from "@/lib/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";


type Schedule = {
  name: string;
  description: string | null;
  kind: "cron" | "every" | null;
  value: string | null;
  timezone: string | null;
  enabled: boolean;
  available: boolean;
  error: string | null;
  paused: boolean;
  next_run_at: string | null;
  running: boolean;
};

type ScheduleStatus = {
  revision: string | null;
  reconciled_revision: string | null;
  schedules: Schedule[];
};

export function AgentSchedulesView({ agentId }: { agentId: string }) {
  const [data, setData] = useState<ScheduleStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    const response = await apiFetch(`/api/agents/${agentId}/schedules`);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail ?? "Could not load schedules.");
    setData(body as ScheduleStatus);
  }, [agentId]);

  useEffect(() => {
    let current = true;
    apiFetch(`/api/agents/${agentId}/schedules`)
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail ?? "Could not load schedules.");
        return body as ScheduleStatus;
      })
      .then((body) => {
        if (current) setData(body);
      })
      .catch((reason) => {
        if (current) {
          setError(reason instanceof Error ? reason.message : "Could not load schedules.");
        }
      });
    return () => {
      current = false;
    };
  }, [agentId]);

  const setPaused = async (schedule: Schedule) => {
    setBusy(schedule.name);
    setError("");
    try {
      const action = schedule.paused ? "resume" : "pause";
      const response = await apiFetch(
        `/api/agents/${agentId}/schedules/${encodeURIComponent(schedule.name)}/${action}`,
        { method: "POST" },
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail ?? `Could not ${action} schedule.`);
      setData(body as ScheduleStatus);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update schedule.");
    } finally {
      setBusy("");
    }
  };

  if (!data && !error) {
    return <Skeleton className="h-64 w-full" />;
  }

  if (!data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Schedules unavailable</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }

  return (
    <section className="flex w-full flex-col gap-4">
      <header className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold">Schedules</h2>
          <p className="truncate text-sm text-muted-foreground">
            From storage revision {data.revision?.slice(0, 12) ?? "none"}
            {data.revision !== data.reconciled_revision ? " · reconciling" : ""}
          </p>
        </div>
        <Button variant="outline" size="icon-sm" onClick={() => void load()} aria-label="Refresh schedules">
          <RefreshCwIcon />
        </Button>
      </header>
      {error ? (
        <Alert variant="destructive">
          <AlertTitle>Schedule operation failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      {data.schedules.length ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {data.schedules.map((schedule) => (
            <Card key={schedule.name}>
              <CardHeader>
                <div className="flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <CardTitle className="font-mono text-sm">{schedule.name}</CardTitle>
                    <CardDescription>{schedule.description ?? "No description"}</CardDescription>
                  </div>
                  <Badge variant={schedule.error ? "destructive" : schedule.paused ? "outline" : "secondary"}>
                    {schedule.error ? "Invalid" : schedule.paused ? "Paused" : schedule.running ? "Running" : "Scheduled"}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                  {schedule.kind && schedule.value ? (
                    <Badge variant="outline" className="font-mono">
                      {schedule.kind === "cron" ? schedule.value : `every ${schedule.value}`}
                    </Badge>
                  ) : null}
                  {schedule.timezone ? <Badge variant="outline">{schedule.timezone}</Badge> : null}
                </div>
                {schedule.next_run_at ? (
                  <p className="text-xs text-muted-foreground">
                    Next run <time dateTime={schedule.next_run_at}>{new Date(schedule.next_run_at).toLocaleString()}</time>
                  </p>
                ) : null}
                {schedule.error ? <p className="text-sm text-destructive">{schedule.error}</p> : null}
                {schedule.available && schedule.enabled ? (
                  <Button variant="outline" size="sm" className="self-start" disabled={busy === schedule.name} onClick={() => void setPaused(schedule)}>
                    {schedule.paused ? <PlayIcon data-icon="inline-start" /> : <PauseIcon data-icon="inline-start" />}
                    {schedule.paused ? "Resume" : "Pause"}
                  </Button>
                ) : null}
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon"><Clock3Icon /></EmptyMedia>
            <EmptyTitle>No schedules</EmptyTitle>
            <EmptyDescription>Create schedules/&lt;name&gt;/job.py in Agent files.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </section>
  );
}
