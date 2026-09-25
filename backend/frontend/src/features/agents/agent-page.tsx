import { useEffect, useState, type ReactNode } from "react";
import {
  BookMarkedIcon,
  CheckIcon,
  FolderGitIcon,
  LinkIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  PlusIcon,
  Trash2Icon,
  TriangleAlertIcon,
  XIcon,
} from "lucide-react";

import { AgentColorPicker } from "@/components/agent-color-picker";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { RepositoryView } from "@/features/repository/repository-view";
import { MarkdownText } from "@/features/threads/markdown-text";
import { useApi } from "@/hooks/use-api";
import { api, apiFetch, type Agent, type Job, type Resource } from "@/lib/api";
import type { AgentThreads, RepositoryFile } from "@/lib/api-types";
import type { AccentColor } from "@/lib/agent-colors";
import { reasonMessage } from "@/lib/format";

// The agent's Workspace: its AGENTS.md, resources, and prompt jobs, and its
// directory in the storage repo (agentmesh's Workspace view).
export function AgentPage({
  agent,
  roster,
  warning,
  leading,
  refreshRoster,
  onChange,
  onRemove,
}: {
  agent: Agent;
  roster?: AgentThreads;
  warning?: string;
  leading?: ReactNode;
  refreshRoster: () => Promise<unknown>;
  onChange: (agent: Agent) => void;
  onRemove: () => void;
}) {
  return (
    <Tabs defaultValue="overview" className="flex min-h-0 flex-1 flex-col gap-0">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b px-4">
        {leading}
        <h2 className="min-w-0 flex-1 truncate text-sm font-medium">
          {agent.name} / Workspace
        </h2>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="files">Files</TabsTrigger>
        </TabsList>
      </header>
      <TabsContent value="overview" className="min-h-0 flex-1 overflow-y-auto p-6 md:p-10">
        <AgentPane
          agent={agent}
          warning={warning}
          onChange={onChange}
          onRemove={onRemove}
        />
      </TabsContent>
      <TabsContent value="files" className="flex min-h-0 flex-1 flex-col">
        <RepositoryView
          rosters={roster ? [roster] : []}
          refresh={refreshRoster}
          agentId={agent.id}
          title={`agents/${agent.id}`}
        />
      </TabsContent>
    </Tabs>
  );
}

// AGENTS.md is read from main; the agent edits it (ask in a thread).
function AgentsDocument({ agentId }: { agentId: string }) {
  const file = useApi<RepositoryFile>(
    `/api/repository?path=${encodeURIComponent(`agents/${agentId}/AGENTS.md`)}`,
  );
  return (
    <section aria-label="AGENTS.md" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <code className="text-xs text-muted-foreground">AGENTS.md</code>
        <span className="text-xs text-muted-foreground">
          Ask the agent to change it.
        </span>
      </div>
      {file.error ? (
        <p role="alert" className="text-sm text-muted-foreground">
          {file.error.message}
        </p>
      ) : file.data ? (
        file.data.after?.text != null ? (
          <MarkdownText text={file.data.after.text} />
        ) : (
          <p className="text-sm text-muted-foreground">
            {file.data.after?.notice ?? "AGENTS.md is empty."}
          </p>
        )
      ) : (
        <p role="status" className="text-sm text-muted-foreground">
          Loading AGENTS.md…
        </p>
      )}
    </section>
  );
}

// Retire cancels every thread (each sandbox is checkpointed and stopped);
// removing deletes the agent record once it has no chats.
function RetireAgent({
  agentId,
  onRemove,
}: {
  agentId: string;
  onRemove: () => void;
}) {
  const [retiring, setRetiring] = useState(false);
  const [status, setStatus] = useState("");
  async function retire() {
    if (!window.confirm("Retire this agent? Every thread is cancelled.")) return;
    setRetiring(true);
    setStatus("");
    try {
      await api(`/api/agents/${encodeURIComponent(agentId)}/retire`, {
        request_id: crypto.randomUUID(),
      });
      setStatus("Retiring: threads are being cancelled.");
    } catch (reason) {
      setStatus(reasonMessage(reason));
    } finally {
      setRetiring(false);
    }
  }
  return (
    <section aria-label="Retire agent" className="flex flex-wrap items-center gap-2 border-t pt-6">
      <Button variant="outline" size="sm" disabled={retiring} onClick={() => void retire()}>
        {retiring ? "Retiring…" : "Retire agent"}
      </Button>
      <Button variant="ghost" size="sm" onClick={onRemove}>
        <Trash2Icon />
        Remove agent
      </Button>
      {status ? (
        <p role="status" className="w-full text-xs text-muted-foreground">
          {status}
        </p>
      ) : null}
    </section>
  );
}

const resourceIcon = {
  repo: FolderGitIcon,
  reference: BookMarkedIcon,
  link: LinkIcon,
} as const;

function ResourceCard({ resource }: { resource: Resource }) {
  const Icon =
    resourceIcon[resource.kind as keyof typeof resourceIcon] ?? LinkIcon;
  return (
    <a href={resource.url} target="_blank" rel="noreferrer">
      <Card className="flex-row items-center gap-3 p-3 transition-colors hover:bg-accent/50">
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium">
            {resource.title}
          </span>
          <span className="truncate text-xs text-muted-foreground">
            {new URL(resource.url).hostname}
          </span>
        </div>
      </Card>
    </a>
  );
}

function RepositoryWarning({ warning }: { warning: string }) {
  return (
    <Alert>
      <TriangleAlertIcon />
      <AlertTitle>GitHub access needed</AlertTitle>
      <AlertDescription>{warning}</AlertDescription>
    </Alert>
  );
}

function AgentPane({
  agent,
  warning,
  onChange,
  onRemove,
}: {
  agent: Agent;
  warning?: string;
  onChange: (agent: Agent) => void;
  onRemove: () => void;
}) {
  const [editingDocument, setEditingDocument] = useState(false);
  const [documentName, setDocumentName] = useState(agent.name);
  const [documentColor, setDocumentColor] = useState<AccentColor | null>(
    agent.color,
  );
  const [savingDocument, setSavingDocument] = useState(false);
  const [documentError, setDocumentError] = useState("");
  const [editingResources, setEditingResources] = useState(false);
  const [repos, setRepos] = useState(agent.repos);
  const [links, setLinks] = useState(agent.resources);
  const [kind, setKind] = useState<"repo" | "link">("repo");
  const [resourceTitle, setResourceTitle] = useState("");
  const [url, setUrl] = useState("");
  const [savingResources, setSavingResources] = useState(false);
  const [resourceError, setResourceError] = useState("");
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [jobsLoadError, setJobsLoadError] = useState(false);
  const [jobEditorOpen, setJobEditorOpen] = useState(false);
  const [editingJob, setEditingJob] = useState<Job | null>(null);
  const [jobSchedule, setJobSchedule] = useState("");
  const [jobPrompt, setJobPrompt] = useState("");
  const [jobErrors, setJobErrors] = useState<{
    schedule?: string;
    prompt?: string;
    form?: string;
  }>({});
  const [jobBusy, setJobBusy] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    apiFetch(`/api/agents/${agent.id}/jobs`)
      .then(async (response) => {
        if (!response.ok) throw new Error();
        return (await response.json()) as Job[];
      })
      .then((found) => {
        if (current) setJobs(found);
      })
      .catch(() => {
        if (current) setJobsLoadError(true);
      });
    return () => {
      current = false;
    };
  }, [agent.id]);

  const resources = [
    ...agent.repos.map((repo) => ({
      title: repo,
      url: `https://github.com/${repo}`,
      kind: "repo",
    })),
    ...agent.resources,
  ];

  const startEditingDocument = () => {
    setDocumentName(agent.name);
    setDocumentColor(agent.color);
    setDocumentError("");
    setEditingDocument(true);
  };

  const saveDocument = async () => {
    if (!documentName.trim()) {
      setDocumentError("Title is required.");
      return;
    }
    setSavingDocument(true);
    setDocumentError("");
    try {
      const response = await apiFetch(`/api/agents/${agent.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: documentName,
          ...(documentColor ? { color: documentColor } : {}),
        }),
      });
      if (!response.ok) throw new Error();
      onChange(await response.json());
      setEditingDocument(false);
    } catch {
      setDocumentError("Could not save agent.");
    } finally {
      setSavingDocument(false);
    }
  };

  const startEditingResources = () => {
    setRepos(agent.repos);
    setLinks(agent.resources);
    setResourceError("");
    setEditingResources(true);
  };

  const addResource = (event: React.FormEvent) => {
    event.preventDefault();
    setResourceError("");
    if (kind === "repo") {
      const repo = url.trim();
      if (!/^[^/\s]+\/[^/\s]+$/.test(repo)) {
        setResourceError("Use owner/repo form.");
        return;
      }
      if (!repos.includes(repo)) setRepos([...repos, repo]);
    } else {
      const nextTitle = resourceTitle.trim();
      const nextUrl = url.trim();
      try {
        const parsed = new URL(nextUrl);
        if (!nextTitle || !["http:", "https:"].includes(parsed.protocol)) {
          throw new Error();
        }
      } catch {
        setResourceError("Add a title and a valid http(s) URL.");
        return;
      }
      if (!links.some((resource) => resource.url === nextUrl)) {
        setLinks([...links, { title: nextTitle, url: nextUrl, kind: "link" }]);
      }
    }
    setResourceTitle("");
    setUrl("");
  };

  const saveResources = async () => {
    setSavingResources(true);
    setResourceError("");
    try {
      const response = await apiFetch(`/api/agents/${agent.id}/resources`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repos, resources: links }),
      });
      if (!response.ok) throw new Error();
      onChange(await response.json());
      setEditingResources(false);
    } catch {
      setResourceError("Could not save resources.");
    } finally {
      setSavingResources(false);
    }
  };

  const closeJobEditor = () => {
    setJobEditorOpen(false);
    setEditingJob(null);
    setJobSchedule("");
    setJobPrompt("");
    setJobErrors({});
  };

  const openJob = (job: Job | null) => {
    setJobEditorOpen(true);
    setEditingJob(job);
    setJobSchedule(job?.schedule ?? "0 9 * * 1-5");
    setJobPrompt(job?.prompt ?? "");
    setJobErrors({});
  };

  const saveJob = async (event: React.FormEvent) => {
    event.preventDefault();
    setJobBusy("save");
    setJobErrors({});
    try {
      const path = editingJob
        ? `/api/jobs/${editingJob.id}`
        : `/api/agents/${agent.id}/jobs`;
      const response = await apiFetch(path, {
        method: editingJob ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ schedule: jobSchedule, prompt: jobPrompt }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const details = Array.isArray(body.detail) ? body.detail : [];
        setJobErrors({
          schedule: details.find((item: { loc?: string[] }) => item.loc?.at(-1) === "schedule")?.msg,
          prompt: details.find((item: { loc?: string[] }) => item.loc?.at(-1) === "prompt")?.msg,
          form: details.length ? undefined : body.detail ?? "Could not save job.",
        });
        return;
      }
      const saved: Job = await response.json();
      setJobs((current) =>
        editingJob
          ? (current ?? []).map((job) => (job.id === saved.id ? saved : job))
          : [...(current ?? []), saved],
      );
      closeJobEditor();
    } catch {
      setJobErrors({ form: "Could not save job." });
    } finally {
      setJobBusy(null);
    }
  };

  const setJobPaused = async (job: Job) => {
    setJobBusy(job.id);
    setJobErrors({});
    try {
      const response = await apiFetch(`/api/jobs/${job.id}/pause`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: !job.paused }),
      });
      if (!response.ok) throw new Error();
      const updated: Job = await response.json();
      setJobs((current) =>
        current?.map((item) => (item.id === updated.id ? updated : item)) ?? null,
      );
    } catch {
      setJobErrors({ form: `Could not ${job.paused ? "resume" : "pause"} job.` });
    } finally {
      setJobBusy(null);
    }
  };

  const deleteJob = async (job: Job) => {
    if (!window.confirm("Delete this scheduled job?")) return;
    setJobBusy(job.id);
    setJobErrors({});
    try {
      const response = await apiFetch(`/api/jobs/${job.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error();
      setJobs((current) => current?.filter((item) => item.id !== job.id) ?? null);
    } catch {
      setJobErrors({ form: "Could not delete job." });
    } finally {
      setJobBusy(null);
    }
  };

  return (
    <div className="mx-auto grid w-full max-w-6xl gap-10 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <section className="mx-auto flex w-full max-w-2xl min-w-0 flex-col gap-6">
        {warning && <RepositoryWarning warning={warning} />}
        {editingDocument ? (
          <FieldGroup>
            <Field data-invalid={Boolean(documentError)}>
              <FieldLabel htmlFor={`agent-name-${agent.id}`}>Title</FieldLabel>
              <Input
                id={`agent-name-${agent.id}`}
                value={documentName}
                onChange={(event) => setDocumentName(event.target.value)}
                aria-invalid={Boolean(documentError)}
              />
            </Field>
            <Field>
              <FieldLabel>Accent color</FieldLabel>
              <AgentColorPicker
                value={documentColor}
                onValueChange={setDocumentColor}
                label={`Accent color for ${agent.name}`}
              />
            </Field>
            <FieldError>{documentError}</FieldError>
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                disabled={savingDocument}
                onClick={() => setEditingDocument(false)}
              >
                <XIcon />
                Cancel
              </Button>
              <Button disabled={savingDocument} onClick={saveDocument}>
                <CheckIcon />
                {savingDocument ? "Saving" : "Save"}
              </Button>
            </div>
          </FieldGroup>
        ) : (
          <>
            <div className="flex items-center justify-between gap-4">
              <h1 className="text-3xl font-semibold tracking-tight">{agent.name}</h1>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Edit agent"
                onClick={startEditingDocument}
              >
                <PencilIcon />
              </Button>
            </div>
            <AgentsDocument agentId={agent.id} />
          </>
        )}
        <RetireAgent agentId={agent.id} onRemove={onRemove} />
      </section>
      <aside className="mx-auto flex w-full max-w-2xl flex-col gap-2 lg:mx-0 lg:max-w-none">
        <div className="flex h-7 items-center justify-between px-1">
          <span className="text-xs font-medium text-muted-foreground">
            Resources
          </span>
          {!editingResources && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Edit resources"
              onClick={startEditingResources}
            >
              <PencilIcon />
            </Button>
          )}
        </div>
        {editingResources ? (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              {repos.map((repo) => (
                <EditableResource
                  key={`repo:${repo}`}
                  resource={{
                    title: repo,
                    url: `https://github.com/${repo}`,
                    kind: "repo",
                  }}
                  onDelete={() => setRepos(repos.filter((item) => item !== repo))}
                />
              ))}
              {links.map((resource, index) => (
                <EditableResource
                  key={`${resource.url}:${index}`}
                  resource={resource}
                  onDelete={() => setLinks(links.filter((_, item) => item !== index))}
                />
              ))}
            </div>
            <form onSubmit={addResource}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor={`resource-kind-${agent.id}`}>Add resource</FieldLabel>
                  <select
                    id={`resource-kind-${agent.id}`}
                    value={kind}
                    onChange={(event) => {
                      setKind(event.target.value as "repo" | "link");
                      setResourceTitle("");
                      setUrl("");
                      setResourceError("");
                    }}
                    className="h-8 rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                  >
                    <option value="repo">GitHub repository</option>
                    <option value="link">Link</option>
                  </select>
                </Field>
                {kind === "link" && (
                  <Field>
                    <FieldLabel htmlFor={`resource-title-${agent.id}`}>Title</FieldLabel>
                    <Input
                      id={`resource-title-${agent.id}`}
                      value={resourceTitle}
                      onChange={(event) => setResourceTitle(event.target.value)}
                      placeholder="Documentation"
                    />
                  </Field>
                )}
                <Field data-invalid={Boolean(resourceError)}>
                  <FieldLabel htmlFor={`resource-url-${agent.id}`}>
                    {kind === "repo" ? "Repository" : "URL"}
                  </FieldLabel>
                  <Input
                    id={`resource-url-${agent.id}`}
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    placeholder={kind === "repo" ? "owner/repo" : "https://example.com"}
                    aria-invalid={Boolean(resourceError)}
                  />
                  {kind === "repo" && (
                    <FieldDescription>Enter a GitHub repository as owner/repo.</FieldDescription>
                  )}
                  <FieldError>{resourceError}</FieldError>
                </Field>
                <Button type="submit" variant="outline">
                  <PlusIcon />
                  Add
                </Button>
              </FieldGroup>
            </form>
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                disabled={savingResources}
                onClick={() => setEditingResources(false)}
              >
                <XIcon />
                Cancel
              </Button>
              <Button disabled={savingResources} onClick={saveResources}>
                <CheckIcon />
                {savingResources ? "Saving" : "Save"}
              </Button>
            </div>
          </div>
        ) : resources.length ? (
          resources.map((resource, index) => (
            <ResourceCard key={`${resource.url}:${index}`} resource={resource} />
          ))
        ) : (
          <span className="px-1 text-sm text-muted-foreground">No resources yet.</span>
        )}
        <Separator className="my-3" />
        <div className="flex h-7 items-center justify-between px-1">
          <span className="text-xs font-medium text-muted-foreground">Jobs</span>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Add job"
            onClick={() => openJob(null)}
          >
            <PlusIcon />
          </Button>
        </div>
        {jobEditorOpen && (
          <form onSubmit={saveJob}>
            <Card size="sm">
              <CardHeader>
                <CardTitle>{editingJob ? "Edit job" : "New job"}</CardTitle>
                <CardDescription>Schedules use UTC.</CardDescription>
              </CardHeader>
              <CardContent>
                <FieldGroup>
                  <Field data-invalid={Boolean(jobErrors.schedule)}>
                    <FieldLabel htmlFor={`job-schedule-${agent.id}`}>Schedule</FieldLabel>
                    <Input
                      id={`job-schedule-${agent.id}`}
                      value={jobSchedule}
                      onChange={(event) => setJobSchedule(event.target.value)}
                      placeholder="0 9 * * 1-5"
                      className="font-mono"
                      aria-invalid={Boolean(jobErrors.schedule)}
                      aria-describedby={
                        jobErrors.schedule
                          ? `job-schedule-help-${agent.id} job-schedule-error-${agent.id}`
                          : `job-schedule-help-${agent.id}`
                      }
                    />
                    <FieldDescription id={`job-schedule-help-${agent.id}`}>
                      Five-field cron expression in UTC.
                    </FieldDescription>
                    <FieldError id={`job-schedule-error-${agent.id}`}>
                      {jobErrors.schedule}
                    </FieldError>
                  </Field>
                  <Field data-invalid={Boolean(jobErrors.prompt)}>
                    <FieldLabel htmlFor={`job-prompt-${agent.id}`}>Prompt</FieldLabel>
                    <Textarea
                      id={`job-prompt-${agent.id}`}
                      value={jobPrompt}
                      onChange={(event) => setJobPrompt(event.target.value)}
                      aria-invalid={Boolean(jobErrors.prompt)}
                      aria-describedby={
                        jobErrors.prompt ? `job-prompt-error-${agent.id}` : undefined
                      }
                    />
                    <FieldError id={`job-prompt-error-${agent.id}`}>
                      {jobErrors.prompt}
                    </FieldError>
                  </Field>
                  <FieldError>{jobErrors.form}</FieldError>
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="ghost"
                      disabled={jobBusy === "save"}
                      onClick={closeJobEditor}
                    >
                      Cancel
                    </Button>
                    <Button type="submit" disabled={jobBusy === "save"}>
                      {jobBusy === "save" ? "Saving" : "Save"}
                    </Button>
                  </div>
                </FieldGroup>
              </CardContent>
            </Card>
          </form>
        )}
        {!jobEditorOpen && jobErrors.form && <FieldError>{jobErrors.form}</FieldError>}
        {jobs === null && !jobsLoadError && (
          <span className="px-1 text-sm text-muted-foreground">Loading jobs…</span>
        )}
        {jobsLoadError && (
          <span className="px-1 text-sm text-destructive">Could not load jobs.</span>
        )}
        {jobs?.map((job) => (
          <Card key={job.id} size="sm">
            <CardHeader>
              <CardTitle className="truncate">{job.prompt}</CardTitle>
              <CardDescription>
                <span className="font-mono">
                  {job.schedule} UTC{job.paused ? " · paused" : ""}
                </span>
                {job.author_display_name ? ` · by ${job.author_display_name}` : ""}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex justify-end gap-1">
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={job.paused ? "Resume job" : "Pause job"}
                disabled={jobBusy === job.id}
                onClick={() => setJobPaused(job)}
              >
                {job.paused ? <PlayIcon /> : <PauseIcon />}
              </Button>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Edit job"
                disabled={jobBusy === job.id}
                onClick={() => openJob(job)}
              >
                <PencilIcon />
              </Button>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Delete job"
                disabled={jobBusy === job.id}
                onClick={() => deleteJob(job)}
              >
                <Trash2Icon />
              </Button>
            </CardContent>
          </Card>
        ))}
        {jobs?.length === 0 && !jobEditorOpen && (
          <span className="px-1 text-sm text-muted-foreground">No jobs yet.</span>
        )}
      </aside>
    </div>
  );
}

function EditableResource({
  resource,
  onDelete,
}: {
  resource: Resource;
  onDelete: () => void;
}) {
  const Icon =
    resourceIcon[resource.kind as keyof typeof resourceIcon] ?? LinkIcon;
  return (
    <Card className="flex-row items-center gap-3 p-3">
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">
        {resource.title}
      </span>
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={`Delete ${resource.title}`}
        onClick={onDelete}
      >
        <Trash2Icon />
      </Button>
    </Card>
  );
}

