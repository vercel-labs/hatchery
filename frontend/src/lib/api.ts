// mirrors backend/models.py

// Streams (chat SSE, terminal websocket) default to same-origin — vercel
// routes /api to the backend service (vercel dev and deployed alike). When
// running bare `next dev` + `uv run dev.py`, set
// NEXT_PUBLIC_BACKEND_ORIGIN=http://127.0.0.1:8000 to dial the backend
// directly: next's dev proxy severs quiet/long sse responses and can't
// upgrade websockets.
const BACKEND_ORIGIN = process.env.NEXT_PUBLIC_BACKEND_ORIGIN ?? "";

export function apiBase(): string {
  return BACKEND_ORIGIN;
}

export function wsBase(): string {
  if (BACKEND_ORIGIN) return BACKEND_ORIGIN.replace(/^http/, "ws");
  return (
    (window.location.protocol === "https:" ? "wss://" : "ws://") +
    window.location.host
  );
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
  pending_space_ids: string[];
  title: string;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  sandbox_id: string | null;
  artifact: string | null;
  created_at: string;
};
