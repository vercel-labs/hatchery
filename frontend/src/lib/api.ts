// mirrors backend/models.py

export type Resource = {
  title: string;
  url: string;
  kind: string;
};

export type Space = {
  id: string;
  name: string;
  goal: string;
  about: string;
  repos: string[];
  resources: Resource[];
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
