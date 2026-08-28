// mirrors backend/models.py

// Chat SSE defaults to same-origin — vercel routes /api to the backend service.
// When running bare `next dev` + `uv run dev.py`, set
// NEXT_PUBLIC_BACKEND_ORIGIN=http://127.0.0.1:8000 to dial the backend directly.
const BACKEND_ORIGIN = process.env.NEXT_PUBLIC_BACKEND_ORIGIN ?? "";

export function apiBase(): string {
  return BACKEND_ORIGIN;
}

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

export type Chat = {
  id: string;
  space_id: string | null;
  title: string;
  topic: string | null;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  sandbox_id: string | null;
  artifact: string | null;
  created_at: string;
};
