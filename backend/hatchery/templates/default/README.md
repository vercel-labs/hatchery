# Agent directory

This directory is one agent's durable workspace. It is mounted at `/workspace/self`
inside the agent's sandbox and committed to the team's shared storage repository.

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Who the agent is and how it works. Loaded into every turn. |
| `USER.md` | Shared team context and interaction defaults. Loaded into every turn. |
| `MEMORY.md` | Task-independent facts and standing conventions. Loaded into every turn. |
| `memories/` | Topic-specific durable notes loaded when relevant. |
| `skills/` | Procedures the agent can load on demand (`<name>/SKILL.md`). |
| `scripts/` | Executable helpers on the agent's `PATH`. |
| `requirements.txt` | Pinned Python dependencies shared by scripts, API routes, and jobs. |
| `api/<path>/route.py` | Public HTTP handlers on the agent's subdomain. |
| `schedules/<name>/job.py` | Durable recurring Python jobs. |
| `lib/` | Python modules shared by scripts, routes, and jobs. |

Everything the agent keeps here is committed on its thread branch and merged into
`main` as is, so scratch files and checkouts belong outside the workspace.
