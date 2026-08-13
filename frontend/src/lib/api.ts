// mirrors backend/models.py

export type Project = {
  id: string;
  name: string;
  goal: string;
  repos: string[];
  created_at: string;
};

export type Chat = {
  id: string;
  project_id: string;
  title: string;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  sandbox_id: string | null;
  artifact: string | null;
  created_at: string;
};
