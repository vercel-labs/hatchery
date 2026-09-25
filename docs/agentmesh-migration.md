# Agentmesh → Hatchery migration plan

Status: planned; no implementation or deployment performed.

Source baseline: local agentmesh checkout at `27242e9acdd7a080542c37cbb1d490cda631bb03`, inspected on 2026-09-25. This plan follows the user's clarified scope, which supersedes the narrower and partly conflicting wording in [direction.md](../direction.md).

## 1. Decisions already made

- Rename spaces to **agents**, throughout the product, API, models, and stores. Do not retain spaces as a second grouping concept.
- Agents remain **shared and named**, not owned by one human. Keep stable agent identity separate from display name, URL slug, and acting human.
- Keep application and execution data in the database. Store the agent's files and their version history in Git.
- Use `vercel-internal-playground/hatchery-storage` as the configured storage repository, through the existing Connect GitHub App. Make repository and canonical branch configuration explicit.
- Each agent has a directory containing `AGENTS.md`. Port all implemented agentmesh workspace capabilities now, not just this one file.
- Keep Hatchery's FastAPI service, Python AI SDK, Rotor, Vercel Sandbox, queues, fx workers, Vite, TanStack Router, and Base UI components.
- Merge agentmesh capabilities into those systems. Do not host a second agentmesh application or model runtime alongside Hatchery.
- Port both trees: the agent/task hierarchy and live sandbox filesystem tree. Port repository browsing, diffs, review, and the rest of the operator experience.
- Copy agentmesh's Git lifecycle and publication policies rather than designing a replacement.
- Start a sandbox before the first model turn, not when someone merely opens an empty composer. Use agentmesh's idle/recovery behavior.
- Start fresh. No legacy data import, backfill, ID translation, or compatibility layer is required.
- Port all implemented agentmesh features, including APIs, schedules, skills, scripts, secrets, budgets, compaction, templates, CLI, and setup. Preserve existing Hatchery capabilities.

No further product decision blocks this plan. Technical uncertainties are explicit implementation gates below; they are not permission to quietly drop features.

## 2. Evidence and scope boundaries

Agentmesh's implemented foundation includes the workspace engine, hierarchy, context, budgets, serving, schedules, CLI, and console. Its own roadmap still marks production certification incomplete. Port behavior and tests; do not assume a copied component is production-proven. See [roadmap](../.reference/agentmesh/lat.md/roadmap.md).

Key differences requiring adaptation:

- Hatchery's dispatcher currently cannot inspect sandbox files and coordinates separate fx conversations. Agentmesh threads directly operate their own workspace. See [dispatcher](../backend/hatchery/agent/dispatcher.py) and [agentmesh thread](../.reference/agentmesh/src/agentmesh/agent/thread.py).
- Hatchery's worker protocol exposes launch/input/cancel and output/transcript/completion events, not the full lifecycle needed for shared budgets and nested task control. See [protocol](../backend/hatchery/worker/protocol.py).
- The current daemon treats an fx turn ending as task completion. Agentmesh distinguishes an idle conversation from an explicitly completed assignment. See [daemon](../backend/hatchery/worker/daemon/main.py) and [runtime contract](../.reference/agentmesh/docs/runtime.md).
- Agentmesh's owner is a human identity. Here the corresponding scope is a shared agent ID; human identities remain attribution and credential subjects.
- Agentmesh's source contains features absent from, or newer than, some prose guides. Resolve disagreements using implementation and behavioral tests. Do not copy stale lifecycle text literally.

Out of scope: agentmesh's own deferred features, such as arbitrary peer-agent messaging, private Git memory, and additional production sandbox providers. Existing Hatchery Slack/GitHub support and shared agents remain in scope even where agentmesh calls those future work.

## 3. Target architecture

### 3.1 One execution system

Use Hatchery's existing execution roles:

1. **Named-agent supervision:** deterministic routing, shared budget, task relationships, and lifecycle coordination on the existing Rotor worker. Port agentmesh's supervisory responsibilities; this layer does not run another model loop.
2. **Root chat:** the current durable Hatchery dispatcher, extended with its own workspace, sandbox tools, context preparation, signals, compaction, and publication.
3. **Delegated task:** an fx session managed by Hatchery, extended with the same workspace and task lifecycle contracts.
4. **Serving:** a separate sandbox per agent for published Python routes and scheduled jobs. It is not a coding worker and has no model loop.

A root chat and its descendants form one canonical task tree. Keep the existing chat ID for channel bindings and transcripts; represent root/child execution relationships explicitly rather than inferring them from sandbox membership. Do not create a second disconnected tree for native fx children.

Every writable execution workspace has one owning execution. Parent and child do not concurrently modify the same agent-file tree. A shared agent's conversations share integrated memory through Git, not through one writable sandbox.

Sources: [agentmesh architecture](../.reference/agentmesh/lat.md/architecture.md), [agent supervisor](../.reference/agentmesh/src/agentmesh/agent/agent.py), [Hatchery Rotor wiring](../backend/hatchery/agent/runtime.py).

### 3.2 Data ownership

| Database: authoritative operational state | Git: authoritative agent files |
| --- | --- |
| Agent ID, name, slug, color, resource/repo associations, status, settings | `AGENTS.md`, shared operating context, core and topic memory |
| Users, sessions, connection references, acting identity, audit attribution | Skills, scripts, shared Python libraries, pinned dependencies |
| Chats, complete transcripts, events, channel bindings, request deduplication | Python API routes and recurring job declarations |
| Rotor state, task tree, assignment status, pending inputs, signals | Shared wiki, team prompt, team skills, starter templates |
| Worker/session references, leases, budgets, usage, grants, cleanup state | Thread branches, checkpoints, proposals, merge ancestry |
| Encrypted secrets, secret requests, schedule pause overrides and run results | No secrets, credentials, runtime journals, or worker records |
| Git operation IDs, revision pointers, review audit/projections | Proposal contents and historical file snapshots |

Do not keep separately editable DB notes and Git memory. Move the notes capability to Git files; do not migrate old notes. The old freeform space description becomes agent instructions, not a competing DB prompt body.

File declarations, such as `SCHEDULE`, remain in Git. Derived availability, next occurrence, pauses, and results belong in DB. Configuration that controls privileged publication, credentials, or budgets is operator-managed, not silently overridden by agent-edited files.

Sandbox files are working copies. `scratchpad/`, coding checkouts, fx session artifacts, and serving data are separate from curated agent memory. Preserve agentmesh's serving-local `/workspace/data` capability, including its documented loss on sandbox destruction; it is not a replacement for Hatchery's DB or a guarantee of durable application storage. Do not introduce a new general-purpose database SDK as part of this port. Workloads needing durable application data must use an explicit database integration.

Sources: [Hatchery models](../backend/hatchery/models.py), [workspace contract](../.reference/agentmesh/docs/workspace.md), [handler data contract](../.reference/agentmesh/docs/api-routes.md).

### 3.3 Repository and sandbox layout

Adapt agentmesh's path mapping, not its Git algorithm. Use a stable directory key independent of an agent's display name:

```text
hatchery-storage/
  templates/default/
    AGENTS.md
    USER.md
    MEMORY.md
    memories/
    skills/
    scripts/
    requirements.txt
  agents/<agent-id>/
    AGENTS.md
    USER.md
    MEMORY.md
    memories/
    skills/<name>/SKILL.md
    scripts/
    requirements.txt
    lib/
    api/<route>/route.py
    schedules/<name>/job.py
  wiki/
    PROMPT.md
    skills/<name>/SKILL.md
    ...
```

- `AGENTS.md` takes the instruction/persona role of agentmesh's `SOUL.md`; avoid two competing persona files.
- `USER.md` means shared team/project operating context here, not the private profile of the first person who creates an agent. UI and templates must explain that.
- Templates seed a new workspace; runtime documentation, SDK, and helper scripts remain packaged with Hatchery and update on deployment.
- Port agentmesh's runtime-only workspace identity marker with Hatchery naming. It is not agent-editable or displayed as user content; the agent registry itself remains DB-owned.
- Creation is a recoverable DB/Git operation. Keep a creating/failed state until the initial directory commit succeeds; retry with the same operation ID.

Thread sandbox mapping:

```text
/workspace/self         agent directory on this execution's branch
/workspace/wiki         shared wiki on this execution's branch
/workspace/collective   other agents' integrated files, read-only
/workspace/scratchpad   disposable local notes
/workspace/repos       independent coding repositories
/workspace/.hatchery    runtime helpers, environment cache, session metadata
```

Coding-repository instructions remain scoped to their repositories. Explicitly test how fx discovers `AGENTS.md` so workspace instructions are neither omitted nor duplicated.

## 4. Feature parity map

Every row must land in code, an operator path where applicable, and behavior-based tests before migration is complete.

| Capability | Reference | Hatchery destination / adaptation |
| --- | --- | --- |
| Creation, templates, configuration validation | `config.py`, `templates/`, `workspace/repo.py` | Shared Agent registry, creation flow, packaged default template and repo initialization |
| Checkpoints, refresh, merge, proposals, GitHub/local review | `workspace/`, `agent/tools.py` | Shared workspace service used by dispatcher and fx tasks |
| Main, thread, full/latest and proposal browsing | `workspace/browser.py`, console repository features | Authenticated repository endpoints and existing router |
| Immediate thread sandbox, idle stop/resume, recovery | `sandbox/`, `agent/thread.py` | Extend current sandbox/worker adapters; separate execution and serving purposes |
| Runtime/team/agent skills; pins and override warnings | `agent/context.py`, runtime assets | Same context rules for root and fx execution |
| Persona, core memory, topic-memory catalog, shared prompt | `agent/context.py`, templates | Git-backed context; `AGENTS.md` naming and shared `USER.md` semantics |
| Helpers, editable scripts, dependency environments | `runtime/`, `sandbox/dependencies.py` | Packaged Hatchery helpers and sandbox preparation |
| Prompt buffering, signals, turn limits, resume | `agent/thread.py`, `messages.py` | Existing durable chat loop and unified task protocol |
| Recursive delegation, parent messages, results, proposals | `agent/agent.py`, `agent/thread.py` | One managed tree with root dispatcher and fx descendants |
| Budgets, grants, daily reset, usage | `agent/budget.py`, `agent/agent.py` | Agent-scoped DB/Rotor supervision covering both execution roles |
| Request sizing, compaction, bounded tool previews | `agent/compaction.py`, `model_budget.py` | Dispatcher context lifecycle and integration with fx's own context handling |
| Public Python handlers and request/response SDK | `serve/`, `sdk/` | Existing FastAPI entrypoint dispatching to isolated serving sandboxes |
| Secret request, inventory, reveal, rotation, deletion | `vault.py`, operator API/UI | Agent-scoped encrypted DB storage; existing operator authentication |
| Git-declared cron/interval jobs | `serve/scheduling.py` | Rotor scheduler with per-agent serving and existing DB execution stores |
| Existing scheduled prompt jobs | Hatchery `store/jobs.py` | Retain as a distinct job kind, not a second scheduler for the same occurrence |
| Tree navigation, activity, queued input, task conversation boards | console thread features | Mounted AppShell; existing streams and TanStack Router |
| Live filesystem tree, previews, diff/review panels | console repository/workspace features | Base UI components; preserve terminals and sandbox controls |
| Routes, schedules, secrets, connections, budget controls | console agent features | Shared-agent configuration and operations pages |
| Archive, unarchive, cancellation, retirement and cleanup | Agent/thread/serve lifecycle | Explicit transitions and recoverable cleanup, not deletion shortcuts |
| Local Git mode, checkout sync, CLI and setup | `cli/`, `workspace/local.py`, `checkout.py` | Hatchery command surface and existing deployment tooling |
| HTTP/operator APIs, follow/reconnect, request IDs | `api/`, `cli/remote.py` | Extend current auth/API/SSE/WebSocket machinery |

The references in this table are relative to [agentmesh's source](../.reference/agentmesh/src/agentmesh) or [console](../.reference/agentmesh/console/src), unless identified as Hatchery.

## 5. Behavior contracts to port

### 5.1 Git lifecycle: port as a unit

1. A root execution branches from canonical `main`; record its exact fork point.
2. Before a model turn, checkpoint any dirty workspace, merge the current upstream, synchronize the effective tree, then load context.
3. Before delegation, checkpoint the parent. The child forks that checkpoint and uses the parent branch as upstream.
4. At idle or completion, checkpoint and consolidate workspace and wiki separately.
5. Children propose to their direct parent; acceptance requires explicit parent approval of the reviewed revision. Root proposals target `main`.
6. Preserve default policies: ordinary workspace changes auto-merge; wiki and serving changes require review. Changes to `api/`, `schedules/`, `lib/`, or root `requirements.txt` select serving review for the entire workspace proposal.
7. Repeated handoffs update the same proposal per section. Empty contributions withdraw pending proposals. Pending review is not a failed handoff.
8. Compatible changes merge through Git. Conflicts return to the originating execution as repairable files. Never publish unresolved conflict markers.
9. Approval validates the expected SHA, section scope, ancestry, and final tree. Stale approval requires re-review.
10. If Git succeeded but sandbox sync failed, mark the sandbox stale and re-materialize from Git before it can export again.

Retain stable operation IDs, compare-and-swap ref updates, bounded retries, uncertain-push/PR recovery, child merge ancestry, safe local checkout sync, and separate workspace/wiki review state. DB/Git/queue/sandbox effects are not one transaction: persist intent and reconcile partial success.

In this plan, `main` means the configured canonical branch. The reference hardcodes `main` in parts of Git and review; configurable staging branches are a necessary Hatchery adaptation, not an existing reference capability. Resolve that branch consistently in checkpoint/refresh, upstream validation, PR targets, browser comparisons, local sync, serving, and schedule reconciliation. Test the complete staging publication cycle against a repository also containing production `main`, asserting production refs never advance.

Keep privileged memory-repository Git operations outside model-controlled coding Git. Port isolated Git configuration/index/object storage and bounded regular-file transfer with executable bits. Retain path validation and exclusions for links, devices, `.git`, and Python caches. Keep platform credentials and runtime files outside exported roots. The reference does not detect arbitrary secrets written into regular agent files, and checkpointing does not honor `.gitignore`; do not present it as a secret scanner. Use the source limits initially: 4,096 files, 4 MiB/file, 32 MiB/tree.

The Changes UI initially covers agent workspace/wiki changes, as agentmesh does. Coding repositories remain visible in the live filesystem and existing PR/terminal surfaces. Do not silently expand this port into a new multi-repository code-review product.

Sources: [workspace implementation](../.reference/agentmesh/src/agentmesh/workspace/repo.py), [Git isolation](../.reference/agentmesh/src/agentmesh/workspace/git.py), [review](../.reference/agentmesh/src/agentmesh/workspace/review.py), [file transfer](../.reference/agentmesh/src/agentmesh/workspace/files.py), [policy defaults](../.reference/agentmesh/src/agentmesh/config.py).

### 5.2 Sandbox and fx execution

- First accepted input from UI, Slack, GitHub, a prompt job, or a handler effect creates/routes a chat. Acquire its sandbox and prepare context before inference. Empty drafts allocate nothing.
- Represent startup explicitly. Retain accepted input across provisioning failure; resume without duplicate chats, sandboxes, or messages.
- Give each managed fx task its own execution workspace, branch, and deterministic sandbox identity, including grandchildren. Retain exact fx session identity across restarts; do not use global `--resume last` as task identity.
- Add direct shell/file/context tools to the dispatcher. Model inference stays in the current durable worker; only operations needing a filesystem run in its sandbox.
- Serialize workspace mutations, refresh, checkpoint, and merge installation. Add quiescence checks for running tools and terminals; copying files over an actively writing process is not safe.
- Checkpoint at handoff, keep idle sandboxes warm for the configured grace period, then stop without destruction. Budget holds and parked executions release promptly.
- Preserve stop versus destroy, confirmed versus uncertain shutdown, and explicit recovery when a retained sandbox no longer exists. Recover Git-backed files; do not pretend lost scratch files or unpushed coding edits can be reconstructed.
- Port command timeouts, output bounds, dependency-install locks, and runtime helper installation. Start with source defaults, adapting only where current platform limits require it.
- Filesystem inspection must not create/reseed a missing sandbox. Bound directories/files and do not follow links; inspection may resume an existing retained sandbox.

### 5.3 Unified task hierarchy

Keep separate fields for execution state (starting/running/idle/held/failed) and assignment state (working/completed/cancelled), plus archive and publication state.

Required task data includes agent/root/parent IDs, depth, parent-local handle, execution kind, exact session ID, workspace refs, completion payload, and durable directed messages. Reuse existing records where possible; avoid duplicate sources of truth.

Port:

- Delegation depth/fan-out limits; source defaults are two delegated levels and four direct children.
- Immutable ordered `message_parent` and `message_task` exchanges.
- Direct-child validation for parent actions; operator access remains governed by Hatchery auth.
- Ordinary child replies visible to the operator without automatically becoming final parent results.
- Explicit completion with summary, result, deliverables, and proposal references, delivered after checkpoint/handoff.
- Reopening completed tasks without rewriting historical completions.
- Subtree cancellation and cleanup; archive only when the subtree is settled and proposals resolved. Unarchive affects the selected root, not every descendant.
- Safe handling of late completion, duplicate events, follow-up input racing with completion, and restarted workers.

Route nested fx delegation through the managed Hatchery API, or register native fx children into this same tree if the supported fx interface permits full lifecycle control. Choose the supported route during the initial integration spike, not two independent systems.

### 5.4 Context, skills, budgets, and compaction

Load effective branch context before each model call, not just at sandbox launch. Port bounded core files, conflict quarantine, changed-file notices, topic-memory descriptions, and the runtime/team/agent skill catalog.

Keep runtime skills non-shadowable, agent overrides of team skills, pinned upstream revisions, changed/removed pin warnings, complete bounded skill reads, support-file inventories, and script-shadow diagnostics. All workspace/repository content remains lower trust than platform instructions.

Keep full transcripts and captured results for operators. Model context is a separately compacted representation. Port prospective request sizing, output reserve, role-safe splits, recent-message retention, skill reload markers, bounded tool previews, context-overflow recovery, and a visible parked state when a request cannot fit.

Add a durable consumed-event cursor to Hatchery before enabling compaction: rebuilding a turn from the complete stored transcript must not reinsert compacted history.

Budgets belong to the shared agent and cover root and descendant main/compaction calls. Port pre-call admission, post-call accounting, idempotent grants, UTC reset, wake-up of held work, and fencing of stale admissions. Preserve the source's cooperative limit: concurrent admitted calls can overshoot; it is not an exact billing cap. Preserve per-execution turn limits separately.

**Early integration gate:** the current fx adapter does not demonstrate pre-model-call context/budget hooks, compaction hooks, or complete usage events. Verify the supported fx interface first. If missing, a compatible fx extension/version is required before full parity can be claimed. A launch-only budget check, prompt instruction, or task-control tool is not equivalent. Do not implement a second external compactor over fx's own context.

Sources: [context](../.reference/agentmesh/src/agentmesh/agent/context.py), [compaction](../.reference/agentmesh/src/agentmesh/agent/compaction.py), [budget supervisor](../.reference/agentmesh/src/agentmesh/agent/agent.py), [current durable loop](../backend/hatchery/agent/durable.py), [current fx protocol](../backend/hatchery/worker/protocol.py).

### 5.5 Published APIs, SDK, secrets, and schedules

Port the handler SDK under Hatchery naming. It should remain a small request/response/job/effect contract, not a client carrying control-plane credentials.

Serving behavior:

- Discover only published `main` code; parse declarations without executing imports during discovery.
- Support the source's GET/POST/PUT/PATCH/DELETE exports, sync/async functions, static/parameter/catch-all paths, precedence rules, invalid-route diagnostics, and response types.
- Preserve repeated headers/query values, raw bodies, JSON helpers, buffered request/response behavior, body/time limits, sanitized failures, and bounded output. Do not promise streaming handlers or arbitrary WebSocket servers.
- Run one isolated serving sandbox per agent, with published code read-only, a writable data area, shared content-addressed dependencies, and a deployment/execution lock.
- Routes and jobs share one execution slot per agent; preserve busy responses and timeout behavior rather than inventing parallel serving.
- Publish after observed/reconciled `main` advancement, without redeploying Hatchery. Show both desired and active revision plus deployment errors.
- `prompt()` produces bounded validated effects delivered durably to the same shared agent. Preserve request/effect IDs and conversation keys; provider retries need explicit deduplication keys where request IDs change.

Public handlers live on separate agent hosts under a configured serving domain, never under Hatchery's authenticated app origin. Preserve host/header/cookie isolation and handler-owned authentication/signature checks. Unknown hosts fail closed; operator APIs are unavailable on serving hosts. Use a separate staging domain for hosted preview acceptance; do not expose production agent routes through arbitrary previews.

Secrets remain encrypted in DB, scoped to agent ID and environment. Port request cards, inventory, set/rotate, explicit authenticated reveal, delete, and retirement. Values must not enter Git, prompts, transcripts, telemetry, or worker command payload logs. Dependency installation receives no invocation secrets. Serving sandboxes receive only the agent's explicit handler secrets, not coding GitHub, queue, model-gateway, or control-plane credentials. Handler code can still leak its own secrets; document this boundary rather than promising impossible redaction.

Scheduling behavior:

- Discover `schedules/<name>/job.py` and literal metadata: five-field cron with optional IANA timezone, or interval of at least one minute; support disable/removal and visible invalid declarations.
- One scheduler per agent and ticker per active job; generation fencing, stable occurrence IDs, revision-pinned execution, at-least-once delivery, overlap skipping, and bounded catch-up.
- Rule changes replace a ticker; unchanged rules keep cadence; code-only changes affect future revisions.
- Persist operator pause independently of Git rule edits. Pausing is not cancellation of an already running occurrence.
- Preserve bounded recent results, failure prompts throttled per job, retries, and protected periodic reconciliation for external merges while idle.
- Keep one-shot signals separate from recurring jobs.
- Retain Hatchery's scheduled prompt capability as another job kind. Each occurrence has exactly one scheduling authority; do not arm both the old cron path and a new ticker for it.

Sources: [HTTP guide](../.reference/agentmesh/docs/api-routes.md), [SDK](../.reference/agentmesh/src/agentmesh/sdk/__init__.py), [serving service](../.reference/agentmesh/src/agentmesh/serve/service.py), [scheduling](../.reference/agentmesh/src/agentmesh/serve/scheduling.py), [vault](../.reference/agentmesh/src/agentmesh/vault.py).

### 5.6 Shared-agent identity and existing integrations

Preserve Hatchery's existing allowlist/login and access checks. Agent sharing does not by itself make every user's chat or credential public. Child conversations inherit the root's access policy. Keep actor attribution for messages, approvals, grants, secret changes, and retirement.

- Storage-repo operations use the configured App identity, scoped to that repository where supported.
- Coding work retains explicit user-delegated authority when requested under a user grant, and explicit App/service authority for unattended work. Missing/revoked grants produce actionable errors, never silent elevation or use of the last speaker's token.
- Carry the selected credential subject through the execution and its descendants. A different human posting a message must not silently change it.
- Serving has its narrower secret-only authority described above.
- Preserve Slack/GitHub routing, webhook deduplication, linked-thread replies, classification onto named agents, user connections, cron triggers, PR tracking, and telemetry.

Verify live storage-repo installation and permissions during deployment preparation. The user's installation statement is intent/context, not a substitute for a successful read/write capability check in the target environment.

Sources: [Hatchery connections](../backend/hatchery/connections.py), [sandbox credentials](../backend/hatchery/worker/sandbox.py), [agentmesh repository identity](../.reference/agentmesh/docs/workspace.md).

### 5.7 Operator UI, CLI, and setup

Port the agentmesh information layout and interaction behavior using Hatchery's components:

- Agent switcher/create/settings; root and recursive child navigation, search revealing ancestors, counted descendant stacks, status/activity chips, and accessible reduced-motion behavior.
- Conversation, optimistic accepted-input reconciliation, queued prompts, timestamp grouping, parent-message attribution, task conversation boards, result/deliverable handoffs, and expandable tool output.
- Resizable independent panes; live Workspace, checkpointed Changes, and State/hierarchy views; keyboard controls and mobile navigation.
- Integrated repository and agent-scoped file browsing, main/thread/proposal selector, full/latest diffs, full-file previews, current merge state, and SHA-pinned approval.
- Budget holds/grants, turn resume, cancellation, archive, connection state, routes, schedules, secret workflows, and retirement confirmation.

Keep AppShell mounted across navigation and preserve drafts/streams. Use TanStack routes and existing same-origin authenticated API/SSE/WebSocket URLs. Keep terminals, SSH, sandbox access, channel links, and PR/artifact links. Do not replace current communication machinery with agentmesh polling/WebSockets just because its UI uses them; add bounded polling only for state not already available in events.

Port CLI capabilities as Hatchery commands: init/check/dev/serve/sync/setup, status/create-or-join/talk/thread/resume/approve/grant/retire/logs, stable request IDs, follow/reconnect, and local Git mode. Use existing Hatchery auth boundaries, not agentmesh's alternate console login. Require reviewed SHA for CLI approval as well as UI approval. Extra CLI parity beyond the reference's implemented commands is not a prerequisite.

Adapt setup's observe/plan/reconcile and interruption recovery to the existing Hatchery project. Reuse the designated storage repo and existing Connect installation; do not create another app deployment by default. Keep software and agent-storage repositories separate. Validation/check mode must not mutate cloud resources.

Sources: [console](../.reference/agentmesh/docs/console.md), [CLI](../.reference/agentmesh/docs/cli.md), [console layout](../.reference/agentmesh/console/src/app/console-layout.tsx), [Hatchery frontend rules](../backend/frontend/AGENTS.md).

## 6. Implementation sequence and gates

Each phase is a coherent change set. Supporting work may proceed in parallel after its contracts are fixed; a phase is complete only after its behavior gate passes.

### Phase 0 — Freeze contracts and prove the fx integration boundary

- Turn the feature map into a checked source → destination → behavioral-test ledger.
- Inventory dependency/API differences against Hatchery's pinned Rotor/AI/Sandbox versions. Do not copy agentmesh's lockfiles or editable sibling dependencies.
- Specify shared-agent identity, task statuses, branch ownership, event IDs, credential attribution, and public-serving boundary.
- Probe supported fx session binding, nested tools, safe context-refresh points, pre-call admission, usage reporting, compaction, and tool-quiescence hooks.
- Choose one managed delegation path; document any required fx version/extension as a prerequisite.

Gate: a small real fx adapter exercise proves the required lifecycle hooks, or reports the exact missing upstream interface. Do not proceed to claims of complete budget/context/task parity on an unproven interface. Git/serving work can proceed independently.

### Phase 1 — Shared Agent model and clean operational namespace

- Replace Space naming across models, stores, classifier, channel routing, APIs, frontend types/routes, jobs, and tests.
- Add agent registry/settings and execution relationships; preserve human attribution separately.
- Add the new DB state and protocol namespaces without reusing incompatible old Rotor processes or queue messages.
- Add recoverable agent/template creation and explicit setup diagnostics.

Gate: two authorized users can work with one named agent under existing access rules; rename/create/retry behavior works; old incompatible IDs fail cleanly rather than crashing the application.

### Phase 2 — Git workspace service and file context

- Port file validation, isolated Git, workspace mapping, local/remote repositories, checkpoint/refresh/proposal/review, browser, and managed checkout sync.
- Add templates, AGENTS/core-memory/skill context, runtime helpers, and dependency preparation.
- Replace DB notes with Git memory operations.
- Test against local bare repositories before touching the remote storage repo.

Gate: replay-safe operations, concurrent root edits, child ancestry, section isolation, conflicts, stale approvals, uncertain remote writes, and Git-success/sandbox-failure repair pass behavioral tests.

### Phase 3 — Automatic root sandbox and dispatcher tools

- Add workspace lifecycle to the existing dispatcher, before first inference from every ingress path.
- Add serialized shell/file/skill tools and context refresh; startup/error/retry projections; idle stop/resume and cleanup.
- Add full-transcript versus model-context separation and a durable ingestion cursor.
- Preserve terminals and existing coding-repository setup without allowing them to race destructive workspace synchronization.

Gate: a root chat edits an agent file, checkpoints, publishes/reviews it, sleeps, resumes, and sees another chat's accepted changes; no explicit create-sandbox request is needed.

### Phase 4 — Hierarchical fx execution and shared runtime policies

- Extend the daemon protocol and exact session persistence; wire fx lifecycle hooks proven in Phase 0.
- Implement managed descendants, directed messages, explicit completion, parent proposals, cancellation/reopen/archive semantics.
- Complete per-call context refresh, shared budget/grants, signals, turn limits, and compaction integration for both roles.
- Preserve complete event history and channel behavior during retries/restarts.

Gate: dispatcher → fx child → fx grandchild completes through one task tree, with branch isolation, parent approval, publication-before-result, budget holds/resume, and restart recovery. Compacted history stays compacted.

### Phase 5 — Operator workspace and hierarchy UI

- Port the layout, both trees, task conversations, state/activity, repository browsing, diffs, review, budget controls, and lifecycle controls.
- Extend the current router/AppShell, not replace them. Add an actual component-test harness; the current test script only discovers `.test.ts` tests.
- Retain drafts, in-flight streams, terminal state, accessible navigation, and narrow-screen behavior.

Gate: component and browser tests cover navigation during streaming, startup/reconnect, child messaging, live-vs-committed files, stale review, and preserved drafts.

### Phase 6 — Isolated serving, SDK, and encrypted secrets

- Port AST discovery, SDK runner/effects, per-agent serving/dependency lifecycle, host isolation, published-revision tracking, and secret workflows.
- Add authenticated operator endpoints and UI for diagnostics, URLs, desired/active revisions, and secrets.
- Verify separate staging host routing before public activation.

Gate: a reviewed Python route becomes live without redeploy; an unreviewed route does not. Public requests cannot reach the control plane, credentials stay out of transcripts, and duplicate effects do not duplicate agent input.

### Phase 7 — Executable schedules and full operations

- Port Git job discovery, scheduler/tickers, pause overrides, revision pinning, history, failure reporting, maintenance, and retirement cleanup.
- Integrate existing prompt jobs as a distinct kind and remove duplicate scheduling paths.
- Finish API/CLI/local setup/check/sync/follow capabilities and configuration documentation.

Gate: cron/timezone/interval jobs, pauses, edits, deleted jobs, missed intervals, retries, serving contention, and retirement behave like the reference; CLI and UI reflect the same state.

### Phase 8 — Fresh-start cutover and live certification

- Remove replaced notes/space/manual-only lifecycle paths and temporary dual implementations.
- Seed fresh shared agents from templates; require no legacy migration.
- Use separate DB/process/queue/sandbox namespaces for preview and cutover. Isolate preview storage branches from production canonical state; preview jobs and public handlers must not consume production authority accidentally.
- Quiesce old ingress/subscribers/jobs before promotion so old deployments cannot act on the new state or continue duplicate schedules.
- Deploy and run the end-to-end scenarios below; inspect traces and retain exact evidence.
- Keep old resources inert for rollback/recovery initially. Explicit old-data deletion is a separate cleanup action, not a prerequisite for launch. Never erase auth/connection state as collateral to discarding old agent records.

Gate: every feature ledger entry is implemented and verified or the migration is explicitly incomplete. Rollback disables new work before restoring the old deployment; it must not reset the shared storage repo or resurrect competing schedulers.

## 7. Likely code ownership

Keep modules small and ownership clear; the following are destinations, not a requirement to reproduce every agentmesh file one-for-one.

| Existing / proposed area | Responsibility |
| --- | --- |
| `hatchery/models.py`, `store/agents.py` (replaces spaces), existing stores | Shared identity and operational records |
| `hatchery/agent/` | Current durable root loop, supervisor, context, compaction, tools, signals, budget |
| `hatchery/workspace/` (new) | Git persistence, review, file validation, browser, checkout sync |
| `hatchery/worker/` | Existing fx adapter/daemon, hierarchical tasks, sessions, events, sandbox lifecycle |
| `hatchery/serve/`, `hatchery/sdk/` (new) | Handler/job discovery, execution, SDK and prompt effects |
| Agent-scoped secret store and vault module | Encryption and authorized secret operations |
| Packaged templates/runtime assets | Default agent files, helpers, skill documentation |
| `hatchery/app/`, existing channels/connections | Authenticated control API, public host dispatch, ingress/egress |
| `frontend/src/app`, feature components, existing routes/lib | Layout and operator parity on existing machinery |
| Hatchery CLI / scripts | Local lifecycle, operator commands, setup/check and deployment validation |

Mirror module structure in tests. Use module imports in Python, preserve locality, and avoid generalized provider/plugin abstractions unless required by the actual port.

## 8. Verification plan

### Deterministic contracts first

- Port meaningful source tests around real local Git, real SDK subprocesses, deterministic clocks/models, and state transitions. Do not count callable-presence checks or mock expectations as feature parity.
- Git: duplicate operations, cross-section/path restrictions, concurrent edits, replay after partial failure, pinned approvals, recursive ancestry, local checkout conflicts.
- Runtime: duplicate inputs, queue ordering, accepted-input recovery, no history resurrection, budget reset/grant races, signals, tool serialization, late worker events, exact session recovery.
- Tasks: nested delegation, immediate directed messages, operator vs parent attribution, explicit completion, reopened work, cancellation and archive races, publication before final result.
- Serving: AST matching/ambiguity, request/response protocol, errors and limits, host/header isolation, published-only code, secret lifecycle, dependency install without secrets, revision changes under load.
- Schedules: cron/timezones including DST, intervals, pause/edit/remove, stable IDs, overlap, bounded catch-up, generation fencing, failure throttles, external-main reconciliation.
- UI: optimistic/queued input, stream reconnect, preserved draft/state, task boards, both trees, diff modes, SHA-pinned review, secrets, schedule controls, keyboard/mobile/reduced motion.

Reference test starting points: [workspace integration](../.reference/agentmesh/tests/integration/test_workspace.py), [thread integration](../.reference/agentmesh/tests/integration/test_thread.py), [gateway integration](../.reference/agentmesh/tests/integration/test_gateway.py), [SDK unit tests](../.reference/agentmesh/tests/unit/test_sdk.py), [context tests](../.reference/agentmesh/tests/unit/test_agent_context.py), [console tests](../.reference/agentmesh/console/tests).

### Commands during implementation

Run focused tests for each touched area first, from `backend/`:

```sh
uv run pytest tests/agent tests/worker
# After the corresponding new test trees exist:
uv run pytest tests/workspace
uv run pytest tests/serve tests/sdk
```

Broaden to `uv run pytest` at shared-integration and final gates after inspecting test environment requirements. Do not assume all tests under `tests/evals` require live models.

For frontend changes, from `backend/frontend/`:

```sh
pnpm test
pnpm lint
pnpm build
```

Extend the test command/harness to run component tests, not silently leave imported TSX scenarios undiscovered. Check new CLI packaging and `--help`/read-only check mode as part of its phase.

### Live acceptance on an isolated preview/staging environment

1. Create a shared named agent. Confirm its Git directory and DB record; edit instructions and verify another authorized operator sees the same agent.
2. Submit UI input. Observe automatic sandbox startup, loaded instructions/skills, file changes, live tree, checkpoint diff, idle stop, and resume.
3. Run child/grandchild fx work. Exchange directed messages, review child changes, complete, reopen, cancel, archive, and resume after a worker restart.
4. Run concurrent chats editing the same agent file. Verify clean merges or visible repairable conflicts, never silent loss.
5. Trigger budget hold and grant, UTC rollover with controlled tests, compaction, large output, and context changes. Confirm full operator transcript survives.
6. Publish/review a handler and a scheduled job. Verify secrets, dependency setup, deployed revision, bounded effects, pause, retries, contention, and external Git merge reconciliation.
7. Verify Slack/GitHub incoming and linked outgoing messages, scheduled prompt jobs, coding GitHub identity, PR tracking, terminals, and same-origin stream reconnects.
8. Exercise revoked grants, provisioning failures, Git/DB outages, lost responses, deployment restart, and stale approval. Accepted work must remain recoverable.
9. Retire a test agent: refuse new work, stop task/scheduler activity, clear appropriate secrets/serving resources, retain documented coding recovery state, and show cleanup failures for retry.
10. Verify preview isolation, promotion, and rollback without competing consumers or public-origin leakage.

Use [agent-browser guidance](use-agent-browser.md) and [Braintrust guidance](use-braintrust.md). Correlate agent/chat/task/worker IDs, Git SHAs, operation IDs, and deployment IDs. Add spans for workspace refresh/checkpoint/merge, publication, serving, scheduling, admission, and cleanup without logging secrets.

## 9. Configuration and deployment prerequisites

Names below are proposed Hatchery names; they are not claims about existing environment variables.

- `HATCHERY_STORAGE_REPO`: intended production value `vercel-internal-playground/hatchery-storage`.
- `HATCHERY_STORAGE_BRANCH`: canonical branch, normally `main`; explicit isolated value for staging.
- Reuse the existing GitHub Connect connector configuration; validate repository access and policy in the deployment environment.
- `HATCHERY_SERVE_DOMAIN`: separate public agent host domain and staging counterpart, with matching routing/DNS configuration.
- A dedicated vault encryption key and stable application/environment identity; document key rotation before production secret use.
- Validated operator settings for models, context/output limits, daily budgets, command/turn/idle limits, delegation limits, review policies, serving timeouts, and revision TTL. Start from reference defaults where compatible; keep current Hatchery model choices unless intentionally changed.
- Versioned worker protocol/queue topics, new Rotor process registrations, protected maintenance, and environment-specific resource naming.
- Pin the fx version supporting the proven adapter contract. Fail startup/check with actionable diagnostics when required capabilities are absent.

No cloud resources, credentials, DNS, remote Git branches, or data were changed while generating this plan.

## 10. Completion criteria and remaining technical gates

The migration is complete when shared agents have the full reference capability set on Hatchery's existing platform, every parity row has behavioral evidence, existing integrations still work, and no hidden second runtime or legacy memory store remains.

Known gates to resolve during implementation:

1. **fx lifecycle interface:** per-call admission/context/usage and managed nested tasks are not proven by the current adapter. This is the first dependency to validate.
2. **Dependency compatibility:** adapt to Hatchery's installed SDK/Rotor interfaces; regenerate necessary durable schemas rather than copying process state or lockfiles.
3. **Cross-system recovery:** Git, DB, queues, and sandboxes need tested reconciliation, not an assumed transaction.
4. **Shared authority:** explicitly selected app/user grants and existing access policy must survive messages, delegation, schedules, and reconnects.
5. **Production certification:** the reference's local tests do not certify hosted routing, microVM lifecycle, scheduling, or cleanup. Those require the staging gates above.

Plan verification is limited to source review and document checks. Runtime tests, builds, remote permission checks, and deployment acceptance have not been run.
