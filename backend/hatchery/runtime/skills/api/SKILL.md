---
name: api
description: Publish HTTP handlers for external webhooks and callbacks on this agent's subdomain.
---

# API routes

Create `api/<path>/route.py` to publish `/api/<path>` after the workspace reaches
`main`. Export one or more uppercase HTTP functions: `GET`, `POST`, `PUT`, `PATCH`,
or `DELETE`. Each receives `hatchery.sdk.Request` and may return
`hatchery.sdk.Response`, `HTMLResponse`, `JSONResponse`, a JSON-compatible
dict/list, text, bytes, or `None`.

```python
"""Receive a completed external job."""

from hatchery.sdk import Response, prompt


def POST(request):
    event = request.json()
    prompt(
        f"External job {event['id']} completed; inspect its result.",
        key=f"job:{event['id']}",
    )
    return Response({"accepted": True}, status=202)
```

## Response formats

Return a dict or list for ordinary JSON, or use `JSONResponse` when the response
should be explicitly JSON or contains another JSON-compatible value. A plain string
is served as `text/plain`; HTML must use `HTMLResponse` so browsers render it instead
of displaying its source.

```python
from hatchery.sdk import HTMLResponse


def GET(request):
    return HTMLResponse(
        """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Hello</title></head>
<body><h1>Hello from Hatchery</h1></body>
</html>"""
    )
```

Both response helpers accept `status=` and repeated `headers=[(name, value), ...]`.
Use `Response` for plain text or bytes and when setting another content type
explicitly.

Use `[name]` for one dynamic path segment and `[...name]` for a final catch-all.
Handlers run with `HATCHERY_DATA` as their working directory and `HOME`, so relative
files and SQLite databases persist across requests, deployments, and sandbox
stop/resume. This state is not Git-backed or replicated and is lost if the serve
sandbox is destroyed. Put shared pinned Python dependencies in root `requirements.txt`.

For webhook signing keys or private API tokens, call
`secret_request(NAME, note)` and tell the operator where to obtain or enter the
value. The value appears to handlers as environment variable `NAME`; you cannot read
it through the tool. Never write secrets into workspace files because Git and other
agents can read them.

Routes are public unless your handler validates a provider signature or authorization
header. Respond quickly and use `prompt()` only when an event needs agent judgment;
irrelevant webhook events should return `204` without waking a thread.

## Prompt identity and conversation routing

Keep these three identities separate:

- `request.id` identifies this HTTP delivery. It comes from `X-Request-ID` when the
  caller supplies one; otherwise Hatchery generates it.
- `prompt(..., key=...)` identifies the external event for idempotency. Reusing a key
  means “this is a retry” and suppresses another Agent message. Omit it to use
  `<request.id>:<effect-position>`, or use a provider event ID such as
  `key=f"stripe:{event['id']}"`. **Never use one constant key for different events.**
- `prompt(..., thread=...)` groups different events into an Agent conversation. It is
  a stable logical alias, not a Rotor thread ID. Omit it to group by request path, so
  every `/api/callback` hit naturally continues one conversation. Set it when events
  need another partition, such as `thread=f"customer:{customer_id}"`. Do not generate
  or persist a thread ID in a file.

Common forms:

```python
# Each HTTP request is delivered once; all hits to this route share a conversation.
prompt(f"Callback: {request.json()}")

# Provider retries of one event are suppressed; distinct events share the route thread.
prompt(f"Stripe event: {event}", key=f"stripe:{event['id']}")

# Distinct events are grouped into one conversation per customer.
prompt(
    f"Customer event: {event}",
    key=f"provider:{event['id']}",
    thread=f"customer:{event['customer_id']}",
)
```

A successful HTTP response does not mean a duplicate prompt created another message;
webhook retries still receive success. When testing, send different `X-Request-ID`
values for distinct events and repeat one value to verify deduplication.

Request query and header collections preserve repeated values and serialize as lists;
use `.get(name)` or `.get_all(name)` for lookup.
