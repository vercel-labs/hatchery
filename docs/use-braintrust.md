# Use Braintrust

Use Braintrust to trace a Hatchery request across the dispatcher, sandbox, subagent, tools, and delivery.

## Find a trace

Ask the user to complete `bt auth login` if the CLI is not authenticated. Then search by a copied `chat_...`, `task_...`, or `wrk_...` ID:

```sh
pnpm --package=braintrust dlx bt view logs \
  --profile "anbuzin's projects" \
  --prefer-profile \
  --project braintrust-coffee-flame \
  --window 1h \
  --search "$ID" \
  --list-mode spans \
  --limit 100 \
  --json
```

Use `--cursor CURSOR` to paginate or widen `--window` when needed. IDs are more reliable than searching prompt text.

## Read the trace

Start at the relevant root span:

- `channel.webhook`: webhook receipt
- `channel.dispatch`: channel routing
- `hatchery.turn`: dispatcher turn
- `sandbox.provision`, `sandbox.prepare`, `sandbox.daemon.repair`: sandbox lifecycle
- `worker.command`: Queue delivery
- `hatchery.agent_run`: fx task
- `fx.tool.call`, `fx.tool.result`: tool activity
- `channel.deliver`: final delivery

Correlate with `chat.id`, `space.id`, `worker.id`, `task.id`, `command.id`, `event.id`, and `queue.message_id`. Check span errors, input/output, deployment ID, environment, and Git commit SHA.

The Hatchery terminal footer provides a copyable JSON block with the active sandbox and task IDs. Ask the user for that block when the failing run cannot be identified.
