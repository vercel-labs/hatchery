// mirrors backend/models.py

import type { AccentColor } from "@/lib/agent-colors";

// Production is same-origin. Vite development dials FastAPI directly so SSE
// and WebSocket connections do not pass through a development proxy.
const BACKEND_ORIGIN =
  import.meta.env.VITE_BACKEND_ORIGIN ??
  (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");

export function apiBase(): string {
  return BACKEND_ORIGIN;
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${BACKEND_ORIGIN}${path}`, { credentials: "include", ...init });
}

function errorDetail(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return null;
  const issues = value.flatMap((issue) => {
    if (!issue || typeof issue !== "object") return [];
    const { loc, msg } = issue as { loc?: unknown; msg?: unknown };
    if (typeof msg !== "string") return [];
    const location = Array.isArray(loc)
      ? loc
          .filter((part) => part !== "body")
          .map(String)
          .join(".")
      : "";
    return [location ? `${location}: ${msg}` : msg];
  });
  return issues.length ? issues.join("; ") : null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// JSON GET, or POST when a body is given; FastAPI `detail` becomes the error message.
export async function api<T>(
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const response = await apiFetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const result = (await response.json().catch(() => ({}))) as {
      detail?: unknown;
    };
    throw new ApiError(
      errorDetail(result.detail) ?? `HTTP ${response.status}`,
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

export function wsBase(): string {
  if (BACKEND_ORIGIN) return BACKEND_ORIGIN.replace(/^http/, "ws");
  return (
    (window.location.protocol === "https:" ? "wss://" : "ws://") +
    window.location.host
  );
}

export type GitHubConnection = {
  id: string;
  login: string;
  avatar_url: string | null;
  installation_id: string | null;
  connected_at: string;
};

export type SlackConnection = {
  team_id: string;
  team: string | null;
  user_id: string;
  user: string | null;
  connected_at: string;
};

export type User = {
  id: string;
  email: string | null;
  name: string | null;
  username: string | null;
  picture: string | null;
  github?: GitHubConnection;
  slack?: SlackConnection;
};

export type Resource = {
  title: string;
  url: string;
  kind: string;
};

export type Agent = {
  id: string;
  name: string;
  repos: string[];
  resources: Resource[];
  color: AccentColor;
  created_at: string;
};

export type AgentWarning = {
  agent_id: string;
  repo: string;
  warning: string;
};

export type Job = {
  id: string;
  agent_id: string;
  author_display_name: string | null;
  schedule: string;
  prompt: string;
  paused: boolean;
};

export type Chat = {
  id: string;
  user_id: string | null;
  author_display_name?: string | null;
  agent_id: string | null;
  title: string;
  topic: string | null;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  parent_chat_id?: string | null;
  sandbox_id: string | null;
  artifact: string | null;
  attention_reason: "result_available" | "blocked" | null;
  archived_at: string | null;
  telemetry_span?: { trace_id?: string } | null;
  created_at: string;
};
