---
name: schedules
description: Run Python jobs on durable cron or interval schedules from this agent's workspace.
---

# Scheduled jobs

Create `schedules/<name>/job.py`. Only direct files with that exact name are
discovered; neighboring Python files are ordinary helpers.

Use this for every recurring task. The `signal(note, delay)` tool is only for a
one-shot follow-up in the current conversation; it does not accept cron rules.

```python
"""Remove stale cache entries each morning."""

from hatchery.sdk import prompt

SCHEDULE = {"cron": "0 8 * * *", "tz": "America/New_York"}


def run(job):
    prompt(f"Cleanup ran for slot {job.scheduled_for}")
    return {"complete": True}
```

Use exactly one literal rule:

- `{"cron": "0 8 * * *", "tz": "America/New_York"}` uses five-field cron.
- `{"every": "4h"}` accepts durations of at least one minute ending in `s`, `m`, `h`, or `d`.

Cron defaults to UTC. Always set an IANA `tz` when the team means a local wall
clock. Set `"enabled": False`, assign `SCHEDULE = None`, comment out `SCHEDULE`,
or delete `job.py` to stop the durable ticker after the change reaches `main`.

`run` may be synchronous or asynchronous. It receives `hatchery.sdk.Job` with
`id`, `name`, `scheduled_for`, and `revision`. Return a JSON-compatible dict/list,
text, bytes, or `None`. Scheduled delivery and retries are at least once; use
`job.id` as an idempotency key for external writes.

Jobs share root `requirements.txt`, owner secrets, and the same persistent
`/workspace/data` as API handlers. Put reusable Python modules in top-level `lib/`
so scripts, API routes, and scheduled jobs can import them. Never store secrets in
workspace files. Use `secret_request` for environment values.

`prompt()` works as it does in API routes. Its default conversation key is
`schedule:<name>`, and its default effect key derives from the occurrence ID.
