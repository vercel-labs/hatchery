// Thread, repository, and sandbox shapes, ported from the agentmesh console and
// mirroring backend/hatchery/app/server.py thread endpoints.

export type Proposal = {
  section: string;
  url: string;
  merged: boolean;
  branch: string;
  sha?: string;
  thread_id?: string;
  reviewer?: "parent";
  merge_sha?: string;
};

export type TaskStatus = "working" | "completed" | "cancelled" | "rejected";

export type Thread = {
  thread_id: string;
  chat_id?: string;
  parent_thread_id: string;
  depth: number;
  upstream: string;
  children: string[];
  status: string;
  live: boolean;
  archived: boolean;
  task_id: string;
  task_handle: string;
  task_objective?: string;
  task_status: TaskStatus | "";
  completion_summary?: string;
  completion_result?: string;
  deliverables: string[];
  summary: string;
  result: string;
  error: string;
  input_tokens: number;
  output_tokens: number;
  proposals: Proposal[];
  activity?: ThreadActivity;
  signals?: Array<{
    id: string;
    note: string;
    at: number;
  }>;
  // Hatchery: a root thread is a chat, titled and badged like one.
  title?: string;
  trigger?: string;
  attention?: "result_available" | "blocked" | null;
};

export type ThreadActivity = {
  status: string;
  phase: string;
  sandbox_active: boolean;
  mailbox_depth: number;
  pending_prompts: number;
  queued_tools: number;
  running_tool: {
    tool_name: string;
    tool_args: string;
  } | null;
  consolidating: string[];
  awaiting_admission: boolean;
  budget_held: boolean;
  quiet_until: number;
  schedules: Array<{
    key: string;
    due_at: number;
  }>;
  updated_at: number;
};

export type AgentBudget = {
  spent: number;
  limit: number;
  remaining: number;
  exhausted: boolean;
  resets_at: number;
};

// GET /api/agents/{id}/threads: agentmesh's per-agent roster entry.
export type AgentThreads = {
  agent_id: string;
  retiring?: boolean;
  waiting: string[];
  budget: AgentBudget | null;
  threads: Thread[];
  tasks?: Record<string, Record<string, unknown>>;
  local_review?: boolean;
};

export type Message = {
  role: string;
  id?: string;
  timestamp?: number;
  request_id?: string;
  turn?: number;
  source?:
    | "operator"
    | "api"
    | "schedule"
    | "signal"
    | "maintenance"
    | "task"
    | "parent";
  task_id?: string;
  task_handle?: string;
  reporting_thread_id?: string;
  task_status?: TaskStatus;
  task_event?: "message" | "completion";
  pending?: "sending" | "queued";
  // Hatchery: who sent a human message, and through which channel.
  author?: string;
  origin?: "slack" | "github" | "ui" | "cron";
  parts?: Array<Record<string, unknown>>;
};

export type PendingPrompt = {
  requestId: string;
  text: string;
  timestamp: number;
};

export type ThreadDetail = Thread & {
  owner: string;
  chat_id: string;
  sandbox?: string;
  turns: number;
  compactions: number;
  messages: Message[];
  revision: number;
  branch: string;
  base_sha: string;
  checkpoint_sha: string;
  tool_progress?: ToolProgress;
};

export type ToolProgress = {
  running: string | null;
  queued: string[];
  results: Array<Record<string, unknown>>;
};

export type Repository = {
  branch: string;
  sha: string;
  main_sha: string;
  base_sha: string;
  merged: boolean;
  summary: string;
  remote: string;
  local_review: boolean;
  review: {
    workspace: "auto" | "review";
    serve: "auto" | "review";
    wiki: "auto" | "review";
  };
  checkout: null | { path: string; status: string; message: string };
  files: Array<{ path: string; mode: string; size: number; oid: string }>;
  changes: Array<{
    path: string;
    status: "added" | "modified" | "deleted";
    before_mode: string | null;
    after_mode: string | null;
  }>;
};

export type FileVersion = {
  text: string | null;
  notice: string | null;
  mode: string;
  size: number;
};
export type RepositoryFile = {
  path: string;
  before: FileVersion | null;
  after: FileVersion | null;
  diff: string | null;
  diff_truncated: boolean;
};

export type SandboxEntry = {
  name: string;
  path: string;
  kind: "file" | "directory" | "symlink" | "other";
};

export type SandboxDirectory = {
  path: string;
  entries: SandboxEntry[];
  truncated: boolean;
};

export type SandboxFile = {
  path: string;
  text: string | null;
  notice: string | null;
  truncated: boolean;
  bytes_read: number;
};

// Serving: GET /api/agents/{id}/routes, /schedules, /secrets (hatchery/serve/api.py).
export type AgentRoute = {
  path: string;
  methods: string[];
  url: string;
  description: string | null;
  available: boolean;
  error: string | null;
};

export type AgentRoutes = {
  revision: string;
  routes: AgentRoute[];
};

export type AgentScheduleRun = {
  name: string;
  scheduled_for: number;
  started_at: number;
  finished_at: number;
  revision: string;
  status: number;
  result: unknown;
  error: string;
};

export type AgentSchedule = {
  name: string;
  description: string | null;
  kind: "cron" | "every" | null;
  value: string | null;
  timezone: string | null;
  enabled: boolean;
  paused: boolean;
  available: boolean;
  error: string | null;
  next_at: number | null;
  running: boolean;
  last_run: AgentScheduleRun | null;
};

export type AgentSchedules = {
  revision: string;
  reconciled_revision: string;
  schedules: AgentSchedule[];
};

export type AgentSecret = {
  name: string;
  stored: boolean;
  updated_at: number | null;
  note: string | null;
};

export type AgentSecrets = {
  secrets: AgentSecret[];
};

export type RevealedAgentSecret = {
  name: string;
  value: string;
};
