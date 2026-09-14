# Use Traces

Use [Traces](https://traces.playground-vercel.tools) to inspect Hatchery chats across classifier, title generation, dispatcher turns, sandbox, subagents, tools, and delivery. Each chat lazily creates one durable `hatchery.chat` root; later turns and serverless invocations append children to that same trace.

## Export configuration

Only future work is exported; there is no historical backfill. Use a new chat for migration verification. Existing chats may have an old root that was never exported to Traces.

- `TRACES_URL` overrides the backend export destination. Set it to an empty string to disable tracing.
- On Vercel (`VERCEL` is set), an unset `TRACES_URL` defaults to `https://traces.playground-vercel.tools`. Preview deployments use this default too and need authorization to production.
- Outside Vercel, tracing requires an explicit `TRACES_URL` and a Hatchery development OIDC token. Obtain the token with `vercel project token hatchery --scope vercel-internal-playground --json` and supply it as `VERCEL_OIDC_TOKEN` without logging it.

The exporter reads the rotating function OIDC token for every HTTP request. It uses synchronous export to preserve invocation context (including queue subscribers and workflow steps), with a five-second export timeout. This adds export latency to span completion but avoids losing request credentials in a background thread. Existing flush calls remain in place. Prompts, outputs, and tool payloads are captured.

The collector must expose authenticated `POST /v1/traces`. A `404` means the collector route is not deployed; a redirect or `401`/`403` means access is not configured. Export errors are logged without failing agent work. The copied link alone does not prove ingestion; verify the trace in Traces.

## Find a trace

In the Hatchery prompt form, use **Copy trace URL** and open the copied link. The URL is `https://traces.playground-vercel.tools/traces/<id>`.

The `<id>` is the OpenTelemetry trace ID: SHA-256 of the UTF-8 AI SDK `trace_...` ID, truncated to the first 16 bytes and encoded as 32 lowercase hex characters. For example, `trace_6b3cf6f282cd` maps to `1a362da7aca90587cb4947ec6b3f54f6`. Use this converted ID for lookup, not the raw `trace_...` ID.

The Traces CLI lives in the sibling `../../traces/cli` project. It uses `VERCEL_OIDC_TOKEN`, or obtains a token through `vercel project token --yes --json` from a linked Traces checkout. Use a Traces-project token for inspection; producer ingestion permission does not imply read permission. Set `TRACES_URL=https://traces.playground-vercel.tools`, then use `uv run --project cli traces ls --search hatchery --limit 20` or `uv run --project cli traces show <id>` from the Traces checkout. Do not paste tokens into docs or chat.

Search currently supports only trace ID, name, and service. It does not search span attributes such as `chat.id`, `task.id`, or `worker.id`, and it does not search prompt text. Prefer the copied trace URL or converted trace ID. Without it, narrow by name (for example, `hatchery.chat`) or service, then inspect candidate traces and their span attributes.

Ingress work before Hatchery identifies a chat, such as `channel.webhook` and `channel.route`, remains a separate trace. Inspect its attributes to correlate it with the chat after routing; chat/task attribute search is not available.

## Read the trace

Start at the relevant span:

- `hatchery.chat`: durable root shared by the whole chat
- `channel.webhook`, `channel.route`: ingress before a chat is known
- `channel.dispatch`: accepted channel message
- `hatchery.classify`, `hatchery.title`: setup LLM work
- `hatchery.turn`: dispatcher turn
- `sandbox.provision`, `sandbox.prepare`, `sandbox.daemon.repair`: sandbox lifecycle
- `worker.command`: Queue delivery
- `hatchery.agent_run`: fx task
- `fx.tool.call`, `fx.tool.result`: tool activity
- `channel.deliver`: final delivery

Within a trace, correlate with `chat.id`, `space.id`, `worker.id`, `task.id`, `command.id`, `event.id`, and `queue.message_id`. These are inspection fields, not supported search filters. Check span errors, input/output, deployment ID, environment, and Git commit SHA.

The Hatchery terminal footer provides a copyable JSON block with the active sandbox and task IDs. Ask the user for that block and the copied trace URL when the failing run cannot be identified.
