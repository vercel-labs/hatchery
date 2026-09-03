"use client";

import { useEffect, useState } from "react";
import { GitBranchIcon, Loader2Icon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { apiBase, apiFetch } from "@/lib/api";

type Launch = {
  title: string;
  repos: string[];
  setup_script: string | null;
  ports: number[];
  branch: string | null;
  git_sha: string | null;
};

const emptyLaunch: Launch = {
  title: "sandbox",
  repos: [],
  setup_script: null,
  ports: [],
  branch: null,
  git_sha: null,
};

export function SandboxForm({
  chatId,
  open,
  onOpenChange,
  onCreated,
}: {
  chatId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (sandboxId: string) => void;
}) {
  const [launch, setLaunch] = useState(emptyLaunch);
  const [suggesting, setSuggesting] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    apiFetch(`/api/chats/${chatId}/sandboxes/suggestion`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not suggest sandbox settings");
        setLaunch(await response.json());
      })
      .catch((reason: Error) => {
        setLaunch(emptyLaunch);
        setError(reason.message);
      })
      .finally(() => setSuggesting(false));
  }, [chatId, open]);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setCreating(true);
    setError("");
    try {
      const response = await apiFetch(`/api/chats/${chatId}/sandboxes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(launch),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.detail?.[0]?.msg ?? body?.detail ?? "Could not create sandbox");
      }
      const sandbox: { id: string } = await response.json();
      onOpenChange(false);
      onCreated(sandbox.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create sandbox");
    } finally {
      setCreating(false);
    }
  };

  const changeOpen = (nextOpen: boolean) => {
    if (!nextOpen) setSuggesting(true);
    onOpenChange(nextOpen);
  };

  return (
    <Sheet open={open} onOpenChange={changeOpen}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>New sandbox</SheetTitle>
          <SheetDescription>
            GPT-5.6 Luna suggested these settings from the space description.
          </SheetDescription>
        </SheetHeader>
        <form className="flex min-h-0 flex-1 flex-col" onSubmit={create}>
          {suggesting ? (
            <FieldGroup className="overflow-y-auto px-4 pb-4" aria-busy="true">
              {["Title", "Repositories", "Setup script", "Ports", "Primary branch", "Primary git SHA"].map(
                (label, index) => (
                  <Field key={label} data-disabled>
                    <FieldLabel>{label}</FieldLabel>
                    <Skeleton className={index === 2 ? "h-32 w-full" : "h-9 w-full"} />
                    {(index === 1 || index === 3) && <Skeleton className="h-4 w-2/3" />}
                  </Field>
                ),
              )}
            </FieldGroup>
          ) : (
          <FieldGroup className="overflow-y-auto px-4 pb-4">
            <Field>
              <FieldLabel htmlFor="sandbox-title">Title</FieldLabel>
              <Input
                id="sandbox-title"
                required
                value={launch.title}
                onChange={(event) => setLaunch({ ...launch, title: event.target.value })}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="sandbox-repos">Repositories</FieldLabel>
              <Textarea
                id="sandbox-repos"
                placeholder="owner/repo, one per line"
                value={launch.repos.join("\n")}
                onChange={(event) =>
                  setLaunch({
                    ...launch,
                    repos: event.target.value.split(/[\n,]/).map((value) => value.trim()).filter(Boolean),
                  })
                }
              />
              <FieldDescription>The first repository is the primary one.</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="sandbox-setup">Setup script</FieldLabel>
              <Textarea
                id="sandbox-setup"
                className="min-h-32 font-mono text-xs"
                placeholder="Optional shell commands"
                value={launch.setup_script ?? ""}
                onChange={(event) => setLaunch({ ...launch, setup_script: event.target.value || null })}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="sandbox-ports">Ports</FieldLabel>
              <Input
                id="sandbox-ports"
                placeholder="3000, 8000"
                value={launch.ports.join(", ")}
                onChange={(event) =>
                  setLaunch({
                    ...launch,
                    ports: event.target.value.split(",").map((value) => Number(value.trim())).filter(Number.isInteger),
                  })
                }
              />
              <FieldDescription>Up to four TCP ports.</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="sandbox-branch">Primary branch</FieldLabel>
              <Input
                id="sandbox-branch"
                placeholder="Optional"
                value={launch.branch ?? ""}
                onChange={(event) => setLaunch({ ...launch, branch: event.target.value || null })}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="sandbox-sha">Primary git SHA</FieldLabel>
              <Input
                id="sandbox-sha"
                placeholder="Optional"
                value={launch.git_sha ?? ""}
                onChange={(event) => setLaunch({ ...launch, git_sha: event.target.value || null })}
              />
            </Field>
            {error && (
              error.toLowerCase().includes("connect github") ? (
                <Alert>
                  <GitBranchIcon />
                  <AlertTitle>Connect GitHub</AlertTitle>
                  <AlertDescription className="flex flex-col gap-3">
                    <span>Connect your account to clone repositories, push branches, and create pull requests.</span>
                    <Button
                      size="sm"
                      nativeButton={false}
                      render={<a href={`${apiBase()}/api/connections/github/authorize`} />}
                    >
                      Connect GitHub
                    </Button>
                  </AlertDescription>
                </Alert>
              ) : (
                <FieldError>{error}</FieldError>
              )
            )}
          </FieldGroup>
          )}
          <SheetFooter>
            <Button type="submit" disabled={suggesting || creating || !launch.title.trim()}>
              {(suggesting || creating) && <Loader2Icon className="animate-spin" />}
              {suggesting ? "Suggesting…" : creating ? "Creating…" : "Create sandbox"}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}
