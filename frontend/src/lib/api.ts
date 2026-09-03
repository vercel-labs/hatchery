// mirrors backend/models.py

// Chat SSE defaults to same-origin — vercel routes /api to the backend service.
// When running bare `next dev` + `uv run dev.py`, set
// NEXT_PUBLIC_BACKEND_ORIGIN=http://127.0.0.1:8000 to dial the backend directly.
const BACKEND_ORIGIN = process.env.NEXT_PUBLIC_BACKEND_ORIGIN ?? "";

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

export type SlackConnection = {
  team_id: string;
  team: string | null;
  user_id: string;
  user: string | null;
  connected_at: string;
};

export type VercelCLIConnection = {
  user_id: string;
  username: string | null;
  email: string | null;
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

export type Space = {
  id: string;
  name: string;
  about: string;
  repos: string[];
  resources: Resource[];
  color: string;
  created_at: string;
};

export type SpaceWarning = {
  space_id: string;
  repo: string;
  warning: string;
};

export type Chat = {
  id: string;
  user_id: string | null;
  space_id: string | null;
  title: string;
  topic: string | null;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  sandbox_id: string | null;
  artifact: string | null;
  archived_at: string | null;
  created_at: string;
};
