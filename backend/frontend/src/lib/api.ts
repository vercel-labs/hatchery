// mirrors backend/models.py

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

export type GitHubRepository = {
  full_name: string;
  installation_id: string;
  private: boolean;
};

export type AppSettings = {
  configured: boolean;
  memory_repository: string | null;
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
  slug: string;
  name: string;
  about: string;
  repos: string[];
  resources: Resource[];
  color: string;
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

export type Thread = {
  id: string;
  user_id: string | null;
  author_display_name?: string | null;
  agent_id: string | null;
  title: string;
  topic: string | null;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  sandbox_id: string | null;
  artifact: string | null;
  attention_reason: "result_available" | "blocked" | null;
  archived_at: string | null;
  telemetry_span?: { trace_id?: string } | null;
  created_at: string;
};
