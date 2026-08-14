// mirrors backend/models.py

export type Space = {
  id: string;
  name: string;
  goal: string;
  repos: string[];
  color: string;
  created_at: string;
};

export type Chat = {
  id: string;
  space_id: string;
  title: string;
  trigger: string;
  status: "queued" | "running" | "done" | "failed";
  sandbox_id: string | null;
  artifact: string | null;
  created_at: string;
};
