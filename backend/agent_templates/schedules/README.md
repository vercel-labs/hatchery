# Schedules

Recurring agent work lives in `schedules/<name>/job.py`. Only direct `job.py`
files are discovered. The name uses lowercase letters, numbers, dashes, or
underscores.

```python
"""Check deployment health every morning."""

SCHEDULE = {"cron": "0 8 * * *", "tz": "America/New_York"}
PROMPT = "Check deployment health and report anything actionable."


def run(job):
    return None
```

Use exactly one `cron` or `every` rule. Intervals must be at least one minute.
Set `enabled` to `False`, set `SCHEDULE = None`, or delete the file to retire a
schedule. Discovery is static: `SCHEDULE` and `PROMPT` must be literals, and a
`run` entrypoint marks the conventional job module. Each occurrence sends
`PROMPT` into a durable Agent thread. Runs are at least once, so external writes
must use the occurrence ID for idempotency. Never store credentials here.
