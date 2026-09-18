import { useEffect, useState } from "react";
import {
  ExternalLinkIcon,
  GitBranchIcon,
  RefreshCwIcon,
} from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import {
  apiBase,
  apiFetch,
  type AppSettings,
  type GitHubConnection,
  type GitHubRepository,
} from "@/lib/api";

export function MemoryRepositoryOnboarding({
  github,
  onComplete,
}: {
  github?: GitHubConnection;
  onComplete: (settings: AppSettings) => void;
}) {
  const [repositories, setRepositories] = useState<GitHubRepository[]>([]);
  const [repository, setRepository] = useState("");
  const [loading, setLoading] = useState(Boolean(github));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [refreshGeneration, setRefreshGeneration] = useState(0);

  useEffect(() => {
    if (!github) return;
    let current = true;
    apiFetch("/api/connections/github/repositories")
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            typeof body.detail === "string"
              ? body.detail
              : "Could not load GitHub repositories",
          );
        }
        if (!current) return;
        const found = body as GitHubRepository[];
        setRepositories(found);
        const firstAvailable = found.find((item) => item.private);
        setRepository((selected) =>
          found.some((item) => item.full_name === selected)
            ? selected
            : (firstAvailable?.full_name ?? ""),
        );
      })
      .catch((reason: Error) => {
        if (current) setError(reason.message);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [github, refreshGeneration]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!repository || saving) return;
    setSaving(true);
    setError("");
    try {
      const response = await apiFetch("/api/settings/memory-repository", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repository }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          typeof body.detail === "string"
            ? body.detail
            : "Could not save the memory repository",
        );
      }
      onComplete(body as AppSettings);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not save the memory repository",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="flex min-h-svh items-center justify-center p-6">
      <Card className="w-full max-w-lg">
        <CardHeader>
          <CardTitle>Set up agent memory</CardTitle>
          <CardDescription>
            Hatchery keeps agent instructions, skills, schedules, and memories in
            one private GitHub repository.
          </CardDescription>
        </CardHeader>
        {!github ? (
          <>
            <CardContent>
              <Alert>
                <GitBranchIcon />
                <AlertTitle>1. Install the Hatchery connector</AlertTitle>
                <AlertDescription>
                  Continue to Vercel Connect, then install Hatchery on the private
                  repository you want to use for agent memory. You will return here
                  to select it.
                </AlertDescription>
              </Alert>
            </CardContent>
            <CardFooter>
              <Button
                className="w-full"
                nativeButton={false}
                render={
                  <a
                    href={`${apiBase()}/api/connections/github/authorize`}
                  />
                }
              >
                Install Hatchery on GitHub
                <ExternalLinkIcon data-icon="inline-end" />
              </Button>
            </CardFooter>
          </>
        ) : (
          <>
            <CardContent>
              <form id="memory-repository-form" onSubmit={save}>
                <FieldGroup>
                  <Alert>
                    <GitBranchIcon />
                    <AlertTitle>GitHub connected as @{github.login}</AlertTitle>
                    <AlertDescription>
                      Select the private repository Hatchery should use for memory.
                    </AlertDescription>
                  </Alert>
                  <Field data-invalid={Boolean(error)}>
                    <FieldLabel htmlFor="memory-repository">
                      2. Memory repository
                    </FieldLabel>
                    <Select
                      value={repository || null}
                      onValueChange={(value) => setRepository(value ?? "")}
                      disabled={loading || saving}
                    >
                      <SelectTrigger
                        id="memory-repository"
                        className="w-full"
                        aria-invalid={Boolean(error)}
                      >
                        <SelectValue
                          placeholder={
                            loading ? "Loading repositories…" : "Select a repository"
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {repositories.map((item) => (
                            <SelectItem
                              key={`${item.installation_id}:${item.full_name}`}
                              value={item.full_name}
                              disabled={!item.private}
                            >
                              {item.full_name}
                              {!item.private ? " — public" : ""}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    <FieldDescription>
                      Public repositories cannot be used. Write access is verified
                      when you continue.
                    </FieldDescription>
                  </Field>
                  {error ? (
                    <Alert variant="destructive">
                      <AlertTitle>Could not finish setup</AlertTitle>
                      <AlertDescription>{error}</AlertDescription>
                    </Alert>
                  ) : null}
                  {!loading && repositories.length === 0 && !error ? (
                    <Alert>
                      <AlertTitle>No repositories found</AlertTitle>
                      <AlertDescription>
                        Update GitHub access and install Hatchery on your private
                        memory repository, then refresh this list.
                      </AlertDescription>
                    </Alert>
                  ) : null}
                </FieldGroup>
              </form>
            </CardContent>
            <CardFooter className="flex justify-between gap-3">
              <Button
                variant="outline"
                nativeButton={false}
                render={
                  <a
                    href={`${apiBase()}/api/connections/github/authorize`}
                  />
                }
              >
                Update GitHub access
              </Button>
              {repositories.length === 0 ? (
                <Button
                  type="button"
                  variant="secondary"
                  disabled={loading}
                  onClick={() => {
                    setLoading(true);
                    setError("");
                    setRefreshGeneration((current) => current + 1);
                  }}
                >
                  {loading ? (
                    <Spinner data-icon="inline-start" />
                  ) : (
                    <RefreshCwIcon data-icon="inline-start" />
                  )}
                  Refresh
                </Button>
              ) : (
                <Button
                  type="submit"
                  form="memory-repository-form"
                  disabled={!repository || saving}
                >
                  {saving ? <Spinner data-icon="inline-start" /> : null}
                  Use this repository
                </Button>
              )}
            </CardFooter>
          </>
        )}
      </Card>
    </main>
  );
}
