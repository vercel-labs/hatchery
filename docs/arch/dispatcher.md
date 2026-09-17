# Dispatcher

Each Hatchery chat has one long-lived Rotor `DurableDispatcher` process. Its
checkpoint is the canonical model conversation and its mailbox serializes UI,
Slack, GitHub, cron, and coding-worker wakeups.

A turn follows this durable path:

1. `start_turn` gets or creates the chat process and sends `TurnInput` with a
   source idempotency key.
2. `Generate` performs one model request and checkpoints the complete assistant
   message.
3. Every model tool call runs in a keyed `run_tool` child. Child verdicts are
   joined before a tool message and the next `Generate` are committed.
4. A final `deliver_turn` child projects all new model messages to Hatchery's
   `(chat_id, "messages")` stream in order, mirrors replies to linked channels,
   and completes worker bookkeeping. Delivery retries reuse Slack's
   `client_msg_id`, GitHub hidden delivery markers, and per-binding receipts.
5. A retried `project_turn` child records terminal state before the dispatcher
   becomes idle. Input received
   during a turn waits in the same process mailbox.

Rotor live chunks carry AI SDK events tagged with their turn id. `spool = True`
lets `/api/chat/{chat_id}/stream` replay an in-flight activation after a browser
reconnect. Chunks are provisional; the process checkpoint is authoritative.

The fx coding workers remain separate persistent Vercel Sandboxes. Their Queue
subscriber updates Hatchery task records and sends a stable worker-completion
turn to the owning Rotor process.

## Vercel

`agent.runtime` registers Rotor's execution and maintenance Queue subscribers.
Postgres holds mailboxes, checkpoints, leases, dispatch intents, and stream
spools; Queue messages are only wakeups.

Every promoted deployment must own its Rotor database before it can execute.
The canonical production URL activates on its first app request. Preview and
immutable deployments must call `POST /api/rotor/activate` explicitly with
`Authorization: Bearer $ROTOR_RELEASE_SECRET`; ordinary preview traffic cannot
rebind a copied production database.

The minute `/api/cron` endpoint runs Rotor maintenance in addition to Hatchery's
scheduled-job outbox. Each Vercel project/environment must use an isolated Neon
branch or database. `DATABASE_URL` should be pooled for ordinary serverless
queries; `DATABASE_URL_UNPOOLED` supplies Rotor's direct LISTEN/NOTIFY transport.
