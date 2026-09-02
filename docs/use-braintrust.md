# Observability

## Braintrust

Hatchery exports AI SDK telemetry to Braintrust through OpenTelemetry. Export is disabled unless `BRAINTRUST_API_KEY` and a parent are configured.

```sh
BRAINTRUST_API_KEY=...
BRAINTRUST_PARENT=project_id:...
```

`BRAINTRUST_PROJECT_ID` may replace `BRAINTRUST_PARENT`; Hatchery converts it to `project_id:<id>`.

The service name is `hatchery`. Model and tool content is captured. Every span includes available Vercel deployment ID, environment, and Git commit SHA. Pending spans are flushed at application shutdown and after Queue event handling.

## Traces

Primary spans:

- `channel.webhook`: webhook validation and acknowledgement.
- `channel.dispatch`: channel-to-chat routing.
- `hatchery.classify`: space selection.
- `hatchery.turn`: one dispatcher turn from UI or channel.
- `channel.deliver`: outbound delivery to channel bindings.
- `sandbox.provision`: Vercel Sandbox creation.
- `sandbox.prepare`: sandbox resume and command preparation.
- `sandbox.daemon.repair`: daemon verification or repair.
- `worker.command`: Queue command delivery.
- `hatchery.agent_run`: one fx task, from launch through completion or failure.
- `fx.user`, `fx.tool.call`, `fx.tool.result`, `fx.assistant`, `fx.attention`, and task lifecycle spans: events emitted by fx through the sandbox daemon.

Use `chat.id`, `space.id`, `worker.id`, `task.id`, `command.id`, `event.id`, and `queue.message_id` to correlate work. Agent-run spans also record the fx session ID and transcript, tool-call, and truncation counts.

The task record stores the serialized `hatchery.agent_run` span. Queue subscribers restore it as the parent of fx event spans, update it after each accepted event, and close it on task completion or failure. At-least-once duplicate events are marked with `applied=false` and do not update task state.

Tool calls use OpenTelemetry `gen_ai.tool.*` attributes and Braintrust tool span metadata. User input, assistant output, tool arguments, tool results, questions, and final task results are attached as Braintrust input or output data. Large assistant output is limited to 8 KiB per span.

## UI context

The terminal pane footer shows one JSON context block for the active sandbox and, when selected, its active task. Use the single copy button and include the block when asking an agent to investigate.

```json
{
  "sandbox": {
    "id": "wrk_...",
    "name": "hatchery-wrk_...",
    "status": "running"
  },
  "task": {
    "id": "task_...",
    "title": "...",
    "fx_session_id": "...",
    "status": "running"
  }
}
```

## Braintrust CLI

Run the CLI without installing it globally:

```sh
pnpm --package=braintrust dlx bt <command>
```

Authenticate and inspect profiles:

```sh
pnpm --package=braintrust dlx bt auth login
pnpm --package=braintrust dlx bt auth profiles
```

Read recent traces:

```sh
pnpm --package=braintrust dlx bt view logs \
  --profile "anbuzin's projects" \
  --prefer-profile \
  --project braintrust-coffee-flame \
  --window 24h \
  --limit 25 \
  --json
```

Use `--window 1h`, `--search TEXT`, `--list-mode spans`, and `--cursor CURSOR` to narrow or paginate results.
