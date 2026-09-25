# Agentmesh → Hatchery migration plan

Status: phases 1-7 and the code side of 8 implemented in the working tree (see Progress), as version 0.2.0.dev0. The CLI was dropped by user decision. No deployments changed.

Source baseline: local agentmesh checkout at `27242e9acdd7a080542c37cbb1d490cda631bb03`, inspected on 2026-09-25.

## 1. Scope rule

Port what agentmesh implements. Keep what Hatchery already has. Build nothing else.

- A feature is in scope only if agentmesh or current Hatchery already has it.
- Adapt only where the two must fit together: spaces become agents, agents are shared, and there is one app. When the two systems disagree, pick one side. Do not design a third option.
- Agentmesh's own deferred work stays out: peer-agent messaging, private memory, other sandbox providers, specialized templates.
- Hardening agentmesh never had is out too: new audit trails, staging-branch publishing, preview-isolation machinery, new fx integration contracts. Add it later as separate work if it's needed.

## 2. Decisions

- Rename spaces to **agents** throughout the product, API, models, and stores. There is no second grouping concept.
- Agents stay **shared and named**, like spaces today. Agent ID stays separate from display name.
- App and execution data stay in the DB. Agent files and their history live in Git.
- Storage repo: `vercel-internal-playground/hatchery-storage`, reached through the existing Connect GitHub App. The canonical branch is `main`, same as agentmesh.
- Each agent's directory has `AGENTS.md`, which takes the role of agentmesh's `SOUL.md`. All other agentmesh workspace features come too.
- Keep Hatchery's FastAPI service, Python AI SDK, Rotor, Vercel Sandbox, queues, fx workers, Vite, TanStack Router, and Base UI.
- An agentmesh thread becomes a Hatchery durable AI SDK loop. Both are already AI SDK loops on Rotor, so this adds no second runtime.
- Existing fx subagents stay as they are today. They are a Hatchery capability. They are not agentmesh threads and are not in the agent task tree, budget, or compaction.
- Copy agentmesh's Git lifecycle and review policies as they are.
- The thread sandbox starts before the first model turn, as in agentmesh. Idle and recovery behavior follow agentmesh.
- Start fresh. No legacy data import, ID translation, or compatibility layer.

## 3. Removed from the earlier plan

These were neither agentmesh features nor existing Hatchery features:

- **The fx lifecycle gate and everything built on it.** That covered pre-call admission hooks, fx compaction, per-call context refresh, full fx usage accounting, exact fx session binding, and fx child/grandchild trees with their own branches. Agentmesh has no fx. Its budgets, compaction, and delegation apply to its own AI SDK loops, and they will apply to Hatchery's durable loop. This was the only blocker.
- **Configurable canonical and staging branches** (`HATCHERY_STORAGE_BRANCH`, staging publication tests). Agentmesh hardcodes `main`.
- **Separate staging serve domain and preview-host acceptance.** Agentmesh's preview owner header is kept instead.
- **Tool and terminal quiescence checks.** Agentmesh serializes sandbox operations inside a thread. That is kept.
- **Carrying a credential subject through descendants, and audit attribution** for grants, secrets, and retirement. Hatchery's existing coding credentials and message authors are kept.
- **Agent creation state machine** (creating/failed states with operation IDs). Creating an agent commits the template, like agentmesh `join`, and can simply be retried.
- **Templates in the storage repo.** Agentmesh ships templates with the software, so Hatchery will too.
- **Workspace marker files as the agent registry.** The DB is the registry.
- **`init` and scaffolding a new deployable repo.** Agentmesh uses these to create a new mesh deployment. Hatchery is already that deployment. `setup` is kept only to check and reconcile the existing project.
- **Cutover engineering.** That covered versioned worker protocol, preview namespace isolation, quiescing old ingress, a rollback plan, outage drills, and key-rotation docs.

## 4. Target architecture

### 4.1 Execution roles

1. **Agent supervisor:** one Rotor process per agent, ported from agentmesh `Agent`. It routes prompts and task messages, owns the thread tree, and owns the daily budget. It does not call models, Git, or sandboxes.
2. **Thread:** Hatchery's durable dispatcher loop, extended to match agentmesh `AgentThread`. A thread gets its own branch, sandbox, and shell/file/skill tools. It also gets context refresh, compaction, and idle/complete handoff. A root thread is a chat, so the chat ID, channel bindings, and transcript stay. Delegated threads are child threads of the same kind.
3. **fx subagents:** unchanged. The launch, message, and check tools stay. They run in the chat's sandbox and do coding work under `/workspace/repos`.
4. **Serving:** one sandbox per agent for published Python routes and Git schedules. It has no model loop.

Sources: [agentmesh architecture](../.reference/agentmesh/lat.md/architecture.md), [agent supervisor](../.reference/agentmesh/src/agentmesh/agent/agent.py), [agentmesh thread](../.reference/agentmesh/src/agentmesh/agent/thread.py), [Hatchery thread](../backend/hatchery/agent/thread.py).

### 4.2 Data ownership

| Database | Git |
| --- | --- |
| Agents (replace spaces): ID, name, color, repos/resources, settings | `AGENTS.md`, `USER.md`, `MEMORY.md`, `memories/` |
| Users, sessions, connections (existing) | Skills, scripts, `lib/`, `requirements.txt` |
| Chats, transcripts, events, channel bindings (existing) | `api/` routes, `schedules/` jobs |
| Rotor state: thread tree, assignment status, messages, budget | `wiki/` (shared prompt and team skills) |
| Encrypted secrets, schedule pauses and results | Thread branches, checkpoints, proposals |
| Prompt jobs (existing) | No secrets or runtime state |

Notes move to Git memory. Old notes are not migrated. The space description becomes the agent's `AGENTS.md`.

`USER.md` holds shared team context, because agents are shared rather than personal.

### 4.3 Repository and sandbox layout

```text
hatchery-storage/
  agents/<agent-id>/
    AGENTS.md  USER.md  MEMORY.md  memories/
    skills/<name>/SKILL.md  scripts/  requirements.txt  lib/
    api/<route>/route.py
    schedules/<name>/job.py
  wiki/
    PROMPT.md
    skills/<name>/SKILL.md
```

The default template, runtime skills, helper scripts, and SDK ship with Hatchery.

Thread sandbox, same as agentmesh with Hatchery naming:

```text
/workspace/self         agent directory on this thread's branch
/workspace/wiki         shared wiki on this thread's branch
/workspace/collective   other agents on main, read-only
/workspace/scratchpad   disposable, not checkpointed
/workspace/repos        coding repositories (thread and fx subagents)
/workspace/.hatchery    runtime helpers, venvs
```

## 5. Feature map

Every row lands in code and gets tests adapted from the listed agentmesh tests before the migration is done. Source paths are relative to [agentmesh src](../.reference/agentmesh/src/agentmesh). Test paths are relative to [agentmesh](../.reference/agentmesh).

| Capability | Agentmesh source | Hatchery destination | Reference tests |
| --- | --- | --- | --- |
| Agent create, templates, config | `config.py`, `templates/`, `workspace/repo.py` | `store/agents.py` (replaces spaces), packaged template | `tests/unit/test_config.py`; `tests/integration/test_workspace.py` |
| Checkpoint, refresh, merge, proposals, review | `workspace/`, `agent/tools.py` | `hatchery/workspace/` | `tests/integration/test_workspace.py`, `test_github_review.py`; `tests/unit/test_review.py`, `test_git_runtime.py` |
| Repository browsing (main/thread/proposal) | `workspace/browser.py` | API endpoints + router | `tests/integration/test_repository_browser.py`; `console/tests/repository.test.tsx` |
| Thread sandbox start, idle stop, resume, recovery | `sandbox/`, `agent/thread.py` | `hatchery/worker/sandbox.py`, durable loop | `tests/integration/test_thread.py`; `tests/unit/test_sandboxes.py`, `test_vercel_sandbox.py` |
| Shell/file/skill tools | `agent/tools.py` | durable loop tools | `tests/integration/test_thread.py` |
| Context: persona, memory, skills, pins, wiki prompt | `agent/context.py` | durable loop context | `tests/unit/test_agent_context.py` |
| Helpers, scripts, dependency envs | `runtime/`, `sandbox/dependencies.py` | packaged assets, sandbox prep | `tests/unit/test_vercel_sandbox.py` |
| Input queueing, signals, turn limits, resume | `agent/thread.py`, `messages.py` | durable loop | `tests/integration/test_thread.py`; `console/tests/composer.test.tsx`, `thread-actions.test.tsx` |
| Delegated threads, messages, completion, parent review | `agent/agent.py`, `agent/thread.py` | supervisor + child threads | `tests/integration/test_thread.py` |
| Budget, grants, daily reset | `agent/budget.py`, `agent/agent.py` | supervisor | `tests/integration/test_thread.py` |
| Compaction, request sizing, tool previews | `agent/compaction.py`, `model_budget.py` | durable loop | `tests/unit/test_agent_context.py`; `tests/integration/test_thread.py` |
| Public Python handlers, SDK, prompt effects | `serve/`, `sdk/` | `hatchery/serve/`, `hatchery/sdk/` | `tests/unit/test_serve_routing.py`, `test_serve_host.py`, `test_sdk.py` |
| Secrets vault | `vault.py` | agent-scoped encrypted store | `tests/unit/test_vault.py` |
| Git schedules | `serve/scheduling.py` | Rotor tickers | `tests/unit/test_serve_scheduling.py` |
| Prompt jobs (existing) | Hatchery `store/jobs.py` | kept as-is | `backend/tests/store/test_jobs.py` |
| Thread tree, activity, task conversations | `console/src/features/threads/` | AppShell + existing streams | `console/tests/thread-navigation.test.tsx`, `thread-activity.test.tsx`, `subagents.test.tsx`, `task-conversations.test.tsx`, `conversation.test.tsx`, `transcript.test.tsx` |
| Live files, changes, diff/review panes, layout | `console/src/features/repository/`, `workspace-panel.tsx`, `console-layout.tsx` | Base UI components; terminals kept | `console/tests/workspace-panel.test.tsx`, `console-layout.test.tsx`, `resize-handle.test.tsx`, `stream-recovery.test.tsx` |
| Agent switcher, API/secrets view, budget notice | `console/src/features/agents/` | agent pages | `console/tests/agent-switcher.test.tsx`, `agent-api-view.test.tsx` |
| Archive, cancel, retire | agent/thread/serve lifecycle | explicit transitions | `tests/integration/test_thread.py`, `test_gateway.py` |
| Operator API, follow, request IDs | `api/` | existing auth/API/SSE | `tests/integration/test_gateway.py`; `tests/unit/test_api.py` |
| ~~CLI remote commands, local checkout sync~~ | ~~`cli/remote.py`, `cli/local.py`, `workspace/checkout.py`~~ | ~~Hatchery CLI~~ (dropped by user decision) | ~~`tests/unit/test_cli.py`~~ |
| ~~`setup` / `check`~~ | ~~`cli/setup/`~~ | ~~check and reconcile the existing project only~~ (dropped with the CLI) | ~~`tests/unit/test_setup.py`~~ |

Existing Hatchery features stay: Slack/GitHub channels, classifier, topic naming, connections, fx subagents, extra sandboxes, terminals/SSH, prompt jobs, PR tracking, and telemetry. Notes are replaced by Git memory.

## 6. Behavior to port

### 6.1 Git lifecycle (port as a unit)

1. A root thread branches from `main` and records its fork point.
2. Before each turn: checkpoint any dirty work, merge upstream, sync, then load context.
3. Before delegating, checkpoint the parent. The child forks from that checkpoint, and its upstream is the parent branch.
4. At idle or complete: checkpoint, then propose the workspace and the wiki separately.
5. Children propose to their parent, and the parent must approve the reviewed revision. Roots propose to `main`.
6. Default policies: ordinary workspace changes auto-merge. Wiki and serving changes need review. Touching `api/`, `schedules/`, `lib/`, or root `requirements.txt` sends the whole workspace proposal to serving review.
7. Conflicts go back to the originating thread as files to fix.
8. Approval checks the expected SHA. A stale approval needs a new review.

Keep agentmesh's operation IDs, compare-and-swap ref updates, isolated Git config, path validation, and file limits (4,096 files, 4 MiB per file, 32 MiB per tree).

Sources: [repo](../.reference/agentmesh/src/agentmesh/workspace/repo.py), [git](../.reference/agentmesh/src/agentmesh/workspace/git.py), [review](../.reference/agentmesh/src/agentmesh/workspace/review.py), [files](../.reference/agentmesh/src/agentmesh/workspace/files.py), [workspace guide](../.reference/agentmesh/docs/workspace.md).

### 6.2 Threads and sandboxes

- The first accepted input from any ingress (UI, Slack, GitHub, prompt job, handler effect) starts the thread sandbox before inference. An empty draft allocates nothing.
- Sandbox operations run serially within a thread. Command timeouts, output limits, and dependency locks follow agentmesh defaults.
- On idle, keep the sandbox warm for the grace period, then stop it without destroying it. Stop and destroy stay distinct. If a sandbox is lost, rebuild the Git-backed files from Git.
- Delegation limits come from agentmesh config (`max_delegation_depth`, per-thread fan-out).
- Tasks: `message_parent`/`message_task`, explicit completion with result, reopening, subtree cancel, and archiving only a settled subtree.
- The manual "create sandbox" flow is replaced by automatic start. The dispatcher's extra-sandbox tool and terminals stay.

### 6.3 Context, budget, compaction

- Load context from the branch before every model call: `AGENTS.md`, memory, wiki prompt, the skill catalog (runtime, team, agent), pins, and change notices.
- The budget belongs to the agent and covers thread main calls and compaction calls. It has pre-call admission, grants, and a UTC daily reset. fx usage is not counted, as today.
- Compaction follows agentmesh: request sizing, output reserve, recent-message retention, and bounded tool previews. Operators still see the full transcript. Hatchery needs a stored cursor so a rebuilt turn doesn't bring compacted history back.

Sources: [context](../.reference/agentmesh/src/agentmesh/agent/context.py), [compaction](../.reference/agentmesh/src/agentmesh/agent/compaction.py), [budget](../.reference/agentmesh/src/agentmesh/agent/budget.py).

### 6.4 Serving, secrets, schedules

- Serve only published `main` code. Discover routes with AST parsing and never import user code during discovery.
- Routes match agentmesh: GET/POST/PUT/PATCH/DELETE; static, `[param]`, and `[...rest]` segments; the same limits, one request at a time per agent, and `/workspace/data`.
- Host routing is `<agent>.<serve-domain>`, with agentmesh's preview header override. Unknown hosts fail closed.
- `prompt()` effects go back to the same agent with idempotency keys.
- Secrets are encrypted in the DB and scoped to an agent. They cover request, list, reveal, rotate, delete, and clearing on retire. Serving sandboxes get only that agent's secrets.
- Schedules: `schedules/<name>/job.py` with cron+timezone or an interval. There is a ticker per job, operator pause, recent results, and maintenance reconciliation.
- Hatchery prompt jobs stay as a separate job kind.

Sources: [serve](../.reference/agentmesh/lat.md/serve.md), [api routes](../.reference/agentmesh/docs/api-routes.md), [schedules](../.reference/agentmesh/docs/schedules.md), [vault](../.reference/agentmesh/src/agentmesh/vault.py).

### 6.5 Identity and integrations

- Keep Hatchery's login, allowlist, and chat access as they are.
- Storage-repo Git uses the Connect App identity, as in agentmesh.
- Coding work keeps Hatchery's existing GitHub connection behavior.
- Slack/GitHub routing, dedupe, linked replies, and classification now target agents instead of spaces.

### 6.6 UI and CLI

- Port the agentmesh console layout and behavior onto Hatchery's AppShell, routes, and Base UI. That means the agent switcher, thread tree, conversation, task boards, workspace/changes/state panes, repository browser, diffs, SHA-pinned approval, budget notice, API/secrets view, and retire.
- Keep Hatchery's streams, terminals, drafts, and channel/PR links. Do not add agentmesh's polling or WebSocket client where Hatchery streams already cover it.
- ~~CLI: `status`, `create` (join), `talk --follow`, `thread`, `grant`, `resume`, `approve`, `retire`, `logs`, `sync`, and `check`/`setup` for the existing project. Mutations accept `--request-id`. Use Hatchery auth.~~ Dropped by user decision; the UI is the only client.

Sources: [console](../.reference/agentmesh/docs/console.md), [cli](../.reference/agentmesh/docs/cli.md), [frontend rules](../backend/frontend/AGENTS.md).

## 7. Phases

Each phase is done when its gate passes.

1. **Agents replace spaces.** Rename across models, stores, classifier, channels, APIs, frontend, jobs, and tests. Add the agent supervisor process. Gate: two users work with one agent, and create/rename work.
2. **Git workspace.** Port `workspace/`, templates, context loading, and runtime helpers. Replace notes with Git memory. Test against local bare repos first. Gate: agentmesh workspace/review tests pass after adaptation.
3. **Thread sandbox and tools.** Add auto-start, shell/file/skill tools, per-turn refresh, idle stop/resume, and the transcript cursor. Gate: a root chat edits an agent file, checkpoints, publishes, sleeps, resumes, and sees another chat's merged change.
4. **Delegation, budget, compaction.** Add child threads, messages, completion, and parent review, plus the budget and grants and compaction. Gate: parent → child → grandchild completes with parent approval. Budget holds and resumes, and compacted history stays compacted.
5. **UI.** Port the console features onto AppShell. Add a TSX component test runner so the ported console tests run. Gate: the ported console tests pass.
6. **Serving and secrets.** Gate: a reviewed route goes live without a redeploy, an unreviewed route does not, and secrets stay out of transcripts.
7. **Schedules~~, CLI, setup~~.** Gate: agentmesh scheduling scenarios pass~~, and the CLI and UI show the same state~~. The CLI and setup were dropped by user decision.
8. **Cutover.** Remove spaces, notes, and manual-sandbox code. Seed fresh agents, deploy, and run live acceptance.

## 8. Verification

- Port meaningful agentmesh tests (real local Git, real SDK subprocesses, scripted models). Don't count mocks or callable-presence checks.
- From `backend/`: `uv run pytest tests/agent tests/worker`, then `tests/workspace`, `tests/serve`, and `tests/sdk` as they appear.
- From `backend/frontend/`: `pnpm test`, `pnpm lint`, `pnpm build`.
- Live acceptance on a preview deployment ([agent-browser](use-agent-browser.md), [Braintrust](use-braintrust.md)):
  1. Create an agent. A second user sees it and edits `AGENTS.md`.
  2. Chat: the sandbox starts, context loads, files change, the diff appears, then idle stop and resume.
  3. Delegate to a child and grandchild: messages, review, complete, cancel, archive.
  4. Two chats edit the same file: a clean merge or a visible conflict.
  5. Budget hold and grant; compaction; large output.
  6. Publish a handler and a schedule: secrets, deps, pause, effects.
  7. Slack/GitHub in and out, prompt jobs, fx subagents, terminals.
  8. Retire a test agent.

## 9. Configuration

- `HATCHERY_STORAGE_REPO` = `vercel-internal-playground/hatchery-storage`.
- The existing GitHub Connect connector. Check read/write access to the storage repo.
- `HATCHERY_SERVE_DOMAIN` plus a wildcard domain (agentmesh `serve.domain`).
- `HATCHERY_SECRETS_KEY` (agentmesh `MESH_SECRETS_KEY`).
- Model, turn, command, idle, delegation, budget, review, and serve limits, starting from agentmesh defaults. Keep Hatchery's current model choices.

## 10. Done when

Every feature-map row is implemented with adapted tests, existing Hatchery features still work, live acceptance passes, and no spaces, notes, or second runtime remain.

Remaining risks, from agentmesh's own roadmap: live Vercel behavior was never certified in agentmesh. That covers sandbox lifecycle, scheduled delivery, and wildcard hosts. The preview acceptance above is the first real check.

## Implementation conventions

Decisions made while implementing, so every phase lands the same way.

- **Agent record**: ID, name, color (one of the 28 accent IDs, nothing else), repos, resources, created time. No `about`: `AGENTS.md` in Git is the description, and create commits the plain template. The classifier routes on name, ID, repos, and resources.
- **Agent ID** is an agentmesh slug (`config.validate_slug`: 1-63 lowercase letters, digits, interior hyphens). It is a Git path segment, a ref segment, and a DNS label for serving. Create takes a display `name` and an optional `id`; without one the ID is slugified from the name. Taken IDs return 409. The seeded default agent is `hatchery`.
- **Module map** (backend/hatchery):
  - `models.py` `Agent` replaces `Space`; `store/agents.py` replaces `store/spaces.py` (table `hatchery_agents`).
  - `config.py`: agentmesh `MeshConfig` sections (model, thread, budget, review, serve limits) with agentmesh defaults, read from env. No `mesh.toml`. Model ID stays Hatchery's.
  - `environment.py`: agentmesh `Mesh` (config, workspace repo, review, sandbox provider, model, clock) with `current()`/`install()`/`use()`.
  - `workspace/`: agentmesh `workspace/` (repo, git, files, review, git_runtime, browser, connect, local). `checkout` (local checkout sync) went with the CLI; `local` stays for tests.
  - `templates/default/`: agentmesh default template with `SOUL.md` renamed `AGENTS.md`.
  - `runtime/`: runtime scripts and skills installed into sandboxes. `runtime.sandbox_files()` is agentmesh `runtime_files()`: the stdlib-only `hatchery/sdk` plus `scripts/*`, installed root-owned under `/workspace/.hatchery/runtime/<digest>` (SDK 0444, scripts 0555) and on `PYTHONPATH`.
  - `worker/provider.py` (agentmesh `sandbox/base.py`), `worker/scripted.py`, `worker/dependencies.py`, `worker/transfer.py`. `worker/sandbox.py` is the one Vercel adapter: Hatchery chat sandboxes plus `VercelSandboxProvider`.
  - `model_budget.py`, `provider_errors.py`, `messages.py`: agentmesh modules of the same names (`Prompt.github_subject` becomes `actor_user_id`).
- **Thread sandbox**: a normal chat sandbox. `acquire(name, purpose="thread", chat=ThreadChat(...))` gets or creates the worker record `name.removeprefix("hatchery-")` owned by that chat, with the daemon, then applies the agentmesh layout and ready marker. Repos clone best effort to `/workspace/repos/<repo>`. Serve sandboxes have no worker record or daemon.
  - `agent/thread.py`: agentmesh `AgentThread` plus Hatchery's chat projection, channel delivery, and tools. Replaces `DurableDispatcher`. Hatchery's part of the system prompt is `context.hatchery_prompt()` with the text in `agent/prompts/hatchery.md` (was `agent/dispatcher.py`).
  - `agent/supervisor.py`: agentmesh `Agent`. `agent/context.py`, `agent/compaction.py`, `agent/budget.py`, `agent/tools.py`, `agent/prompts/`.
  - `serve/`, `sdk/`, `vault.py`: ports of the same agentmesh modules. `cli/` was ported, then removed by user decision.
- **Storage layout**: `agents/<agent-id>/` and `wiki/` on `main`. Agentmesh `workspaces/<owner>` maps to `agents/<agent-id>`. No marker files; the collective view lists `agents/*` directories on `main`.
- **Code style**: Hatchery rules win over agentmesh style. Import modules, not names (except `typing`). No `from __future__ import annotations`. Tests mirror app paths (`hatchery/workspace/repo.py` → `tests/workspace/test_repo.py`).
- Agentmesh `repositories/` is not ported. Coding credentials stay Hatchery's.
- **Runtime (phases 3-4)**: one Rotor `Supervisor` per agent (`key=<agent id>`, `scope="agents"`, `agent/supervisor.py`) spawns `AgentThread` children (`agent/thread.py`). A root thread's key is `chat:<chat id>`; a delegated thread's key is its task id (`<parent task id>:<tool call id>`).
  - Every thread has a chat. A delegated thread creates `chat_<sha256(thread id)[:12]>` (trigger `task`, `parent_chat_id` set). `GET /api/chats` lists root chats; `?parent_chat_id=` lists children.
  - The chat's events stream `thread` holds `{agent_id, thread_id, cursor}`. `supervisor.start_turn` sends a `TurnInput` with only the user messages after the cursor, so compacted history never comes back. The chat's `messages` stream stays the full transcript; the thread's `messages` state is the model history.
  - Hatchery turns map onto thread inputs. A turn streams under its `turn_id` until its answer is final; `finish_turn` persists messages, delivers replies, and projects the end. Work the thread starts itself (signals, task messages, grants, resumes) gets an internal turn (origin `thread`). A budget hold, park, or stop ends the open turn as failed/cancelled; the input stays queued.
  - Hatchery tools run as serial `run_hatchery_tool` children beside `run_bash`. Notes tools are not offered (notes are replaced by Git memory). `secret_request` is inline in the thread and records only name and note on the supervisor.
- **Serving and schedules (phases 6-7)**:
  - `serve/host.py` `HostDispatch` is the outermost middleware of the one FastAPI app: exactly `<agent>.<HATCHERY_SERVE_DOMAIN>` (default `localhost`) goes to `serve/app.py`, everything else to the normal app; malformed or nested agent hosts get 400/421. `X-Hatchery-Agent` (agentmesh's owner header) is honored only when `VERCEL_ENV=preview`.
  - `serve/service.py` reads the agent's directory at current `main` per request (15 s cache, cleared by the main observer) and redeploys the one serve sandbox (`provider.sandbox_name("serve:v1:<env>:<agent>")`, no daemon) when the revision moved. No Hatchery redeploy is needed. Operator endpoints are in `serve/api.py` under `/api/agents/{id}/`: `serve`, `routes`, `schedules`, `schedules/{name}/pause|resume`, `secrets`, `secrets/{name}` (set = rotate), `secrets/{name}/reveal|delete`.
  - `prompt()` effects become normal turns via `supervisor.prompt`: the conversation key (default route path or `schedule:<name>`) names a chat `chat_<sha256[:12]>` (trigger `api`/`schedule`), and the effect key fixes message and turn IDs, so retries add nothing.
  - `vault.py` keeps agentmesh's storage: one `SecretVault` Rotor process per agent (DB-backed) holding AES-GCM envelopes bound to agent and name. Key `HATCHERY_SECRETS_KEY`, or `<data dir>/secrets-key` outside Vercel. Retire clears it and leaves the tombstone that makes serving answer 410.
  - `serve/scheduling.py`: one `Scheduler` per agent (scope `agents`) with a Rotor `Ticker` per job. `runtime.install()` sets `scheduling.observe` as the repo's main observer; `/api/cron` reconciles every fifth minute. Prompt jobs stay in `store/jobs.py`.
  - All serve processes are in `scheduling.PROCESSES`; `agent/runtime.py` registers `supervisor.PROCESSES + scheduling.PROCESSES`. `rotor-schema.json` is `python -m rotor.check hatchery.agent.runtime`.
  - The idle sandbox stop is deferred while an fx task in that sandbox is pending/running/attention or a terminal is open. A running thread sandbox for the same actor is reacquired without another `prepare_for_command`.
  - A chat's agent can change only before its thread starts (409 after).

## Progress

Updated 2026-09-25.

| Phase | Status |
| --- | --- |
| 0 — Inventory | Done |
| 1–2 | Done in the working tree |
| 3 — Thread sandbox and tools | Done: gate in `tests/agent/test_thread.py` |
| 4 — Delegation, budget, compaction | Done: gates in `tests/agent/test_supervisor.py`, `test_budget.py`, `test_compaction.py` |
| 5 — UI | Done in the working tree: ported console tests pass under `pnpm test` (vitest + Testing Library + jsdom beside `tsx --test`); see Phase 5 notes |
| 6 — Serving and secrets | Done in the working tree: gate in `tests/serve/test_service.py` (reviewed route live without a redeploy, unreviewed route 404, secrets out of transcripts and only in their agent's serve sandbox); also `tests/serve/test_routing.py`, `test_host.py`, `test_api.py`, `tests/sdk/test_sdk.py`, `tests/test_vault.py` |
| 7 — Schedules~~, CLI, setup~~ | Done in the working tree: schedules in `tests/serve/test_scheduling.py`. CLI and setup dropped by user decision; see Phase 7 notes |
| 8 — Cutover | Code done in the working tree (see Phase 8 notes). Not deployed; live steps and acceptance open |

Phase 6-7 open items: serving, wildcard hosts, and scheduled delivery were not run on Vercel. Live needs `*.HATCHERY_SERVE_DOMAIN` on the project, `HATCHERY_SERVE_DOMAIN`, and `HATCHERY_SECRETS_KEY` (32 bytes, base64url). The serve runner calls `python3` in the sandbox (agentmesh: `python`).

### Phase 7 notes (CLI and setup)

- The CLI was built, then dropped by user decision. Removed: `hatchery/cli/`, `tests/cli/`, the `hatchery` console script, the `typer` and `watchfiles` deps, `workspace/checkout.py` (local checkout sync) and `WorkspaceRepo`'s managed checkout, the CLI login (`cli_port`/`cli_challenge`, `POST /api/auth/cli`, bearer-header sessions), `request_id` on agent create and approve, and `Agent.request_id`. Web login is exactly as in 0.1.
- `request_id` stays where the UI sends one: grants, retire, resume, stop, schedule pause/resume, and secrets.

### Phase 8 notes

- Removed: the manual sandbox flow (`POST /api/chats/{id}/sandboxes`, `GET /api/sandboxes/suggestion`, `GET /api/chats/{id}/sandboxes/suggestion`, `agent/sandbox.py` `suggest`), `GET /api/chats/{id}/messages` (the UI uses `/transcript`), and the Slack `legacy_token` binding migration. The `create_sandbox`/`list_sandboxes` tools, sandbox listing, terminals, SSH, and fx endpoints stay.
- Storage: old data stays, nothing is dropped or altered. A table keeps its 0.1 name when its schema is unchanged and its old rows can't reach new code; otherwise it gets a `_v2` name (index names too, since Postgres index names are schema-wide):
  - `hatchery_chats_v2`: `space_id` became `agent_id`; old chats would show up in lists.
  - `hatchery_bindings_v2`: old bindings would route Slack/GitHub replies to chats that no longer exist (and `claim` would fail on them).
  - `hatchery_jobs_v2`: `space_id` became `agent_id`; old jobs would run.
  - `hatchery_workers_v2`: old worker records would let 0.1 sandbox daemons' events trigger turns.
  - `hatchery_streams`, `hatchery_events` (and `pg_notify` channel `hatchery_events`): same schema; read only by stream ID, and new stream IDs are fresh.
  - `hatchery_job_executions_v2`: new; same schema, but the 30-day cleanup would otherwise delete old rows, and old data stays untouched.
  - `hatchery_worker_tasks`, `hatchery_worker_terminals`: same schema; read only by fresh IDs, chats, or workers. `worker_event` drops events whose worker is not in `hatchery_workers_v2` before touching tasks (`tests/app/test_server.py::test_worker_event_rejects_old_daemon_whose_worker_is_unknown`).
  - Users, sessions, OAuth states, identities, and dedupe keep their names; `hatchery_agents` is new. The old `ALTER TABLE` lines are gone.
- Rotor has no table prefix or schema option, and Neon's pooler rejects a `search_path` startup parameter, so Rotor keeps the shared `rotor_*` tables and their old rows. Rotor claims and dispatches only registered type names, so old `DurableDispatcher` rows are inert. The two task names the old runtime also spawned stay renamed (`run_tool` → `run_hatchery_tool`, `announce_turn` → `announce_thread_turn`) so old rows can't be claimed; the only remaining shared names are Rotor's `Group` and `Ticker`, which old Hatchery never started.
- Queue topics are the 0.1 names: `hatchery-dispatcher-v1`, `hatchery-dispatcher-maintenance-v1` (consumer group `hatchery-dispatcher-v1`), `hatchery-worker-events-v1` (consumer group `hatchery-control-plane-v1`). A 0.1 daemon's events arrive and are dropped by the worker check above.
- Other old-only code removed: `Agent.about` and its default text, legacy agent colors (aliases and custom values, backend and UI), `WorkerSpec` `vcpus`/`memory` and the `legacy` size label, the list-time Slack title cleanup, the transcript's duplicate-tool-part repair, and `agent/dispatcher.py` (folded into the thread prompt; spans are now `hatchery.thread.turn`/`hatchery.thread.tool`).
- Seed: the first call that lists agents on a fresh deployment (`_agents()` in `app/server.py`) creates `hatchery` and commits the template through the same `join` as `POST /api/agents`. A failed commit removes the row, so the next call retries; a commit that already landed is found (`FileExistsError`) and not repeated. The startup hook no longer creates the row without its files. Gate: `tests/app/test_server.py::test_default_agent_seed_commits_the_template_once_and_retries_after_failure`.
- Wiki: as in agentmesh, `wiki/PROMPT.md` and `wiki/skills/` are optional and nothing seeds them at runtime (agentmesh only has `wiki/README.md` from its `init` scaffold, which §3 drops). A thread on a `main` without `wiki/` works, and the first reviewed wiki proposal creates it. Gate: `tests/agent/test_thread.py::test_fresh_storage_without_wiki_gets_it_from_the_first_reviewed_proposal`.
- Live: the Vercel project `hatchery` deploys from `vercel-internal-playground/hatchery-storage`, a wrapper that pins the `vercel-hatchery` wheel and declares its own queue subscribers. That repo is also the storage repo, so it needs a new wheel (0.2.0.dev0; its topics are unchanged), and agentmesh's `ignoreCommand` (skip builds for thread branches and for `main` commits that only touch `agents/` and `wiki/`), or every agent publish redeploys production.

### Phase 5 notes

- Console features live in `frontend/src/features/{threads,repository,agents}` on AppShell. A root thread is its chat (`/chats/<id>`); the draft at `/` stays mounted through chat creation; Workspace is `/agents/<id>`, the API view `/agents/<id>/api`, Repository `/repository`. The last conversation stays mounted (React `Activity`) under other views.
- Streams: the transcript is `GET /api/chats/{id}/transcript` (stored model messages with time and provenance); the running turn streams through `useChat`; thread details refresh on the chat's SSE and while the turn streams. Only roster activity (5 s) and live sandbox files (2 s, visible tab) poll, since no stream reports them.
- Endpoints: `GET /api/repository` (`chat_id`, `proposal`, `path`, `revision`, `comparison`), `GET /api/chats/{id}/thread/filesystem[/file]`, `GET /api/chats/{id}/transcript`. `GET /api/agents/{id}/threads` and `/api/chats/{id}/thread` add Rotor activity and refreshed proposal `merged`.
- The API view (routes, schedules, secrets) uses the serve endpoints in `hatchery/serve/api.py`; agentmesh's per-agent GitHub connection is not ported (Hatchery keeps its account connection).
- Notes store, API, and UI are removed; the agent page shows `AGENTS.md` from `main`. The manual create-sandbox UI is removed; the thread sandbox and extra sandboxes appear in the Terminal tab.

The earlier fx lifecycle blocker no longer applies (see §3). fx subagents keep the current adapter and its pinned fx 0.0.8.

### Baselines

- Hatchery started clean at `95d77dc5827378e073dd940cc87367fc1a0c4526`.
- Agentmesh at `27242e9acdd7a080542c37cbb1d490cda631bb03`, clean.
- Agentmesh's Rotor checkout matches Hatchery's installed Rotor source byte-for-byte. Persisted state and schemas are still not transferable.

### Dependency differences

| Dependency | Agentmesh | Hatchery | Note |
| --- | --- | --- | --- |
| Python | `>=3.12` | `>=3.14,<3.15` (3.14.7) | Don't copy lockfiles. |
| AI SDK | 0.5.2 | 0.7.0 | APIs used by agentmesh exist; test behavior. |
| Rotor | `rotorcore` 0.0.1 | `rotorcore[vercel]` 0.0.2 | Regenerate schemas. |
| Sandbox | 0.4.0 | 0.6.0 (via `vercel` 0.11.3) | Verify process/timeout behavior. |
| Connect | 0.1.0 | 0.2.0 | Verify adapted calls. |
| Cryptography | 48.x | 50.0.0 (transitive) | Vault needs a direct dependency. |
| modelsdotdev | 0.20260818.0 | 0.20260610.0 | Check context sizing for current models. |
| WebSockets | 17.1 | 16.1.1 (`<17`) | Use Hatchery's version. |
| PyYAML / Typer / Watchfiles | present | PyYAML only | Typer and Watchfiles went with the CLI. |
| Cron | Rotor `Cron`/`Interval`/`Ticker` | `croniter` + Rotor | Prompt jobs keep croniter. |
