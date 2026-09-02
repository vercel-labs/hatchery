# Store

Everything durable lives in `backend/store/`: Neon Postgres when `DATABASE_URL` is set, or JSONL/JSON files under `backend/.data` otherwise. Tests always use files.

The primitive is an append-only stream keyed by `(stream_id, namespace)`. A chat transcript is its `(chat_id, "messages")` stream, with one model message per event as the source of truth. Its worker record is the tail of `(chat_id, "worker")`. Chats, spaces, bindings, and dedupe records are rows alongside streams.

The schema is idempotent DDL created on startup. There are no migrations.
