"""Vercel entrypoint (see [tool.vercel] in pyproject.toml).

A dummy for now: health check plus the channel webhooks. Channels:
- /channels/v1/slack   needs SLACK_CONNECTOR (connect uid, e.g. "slack/fabricator")
- /channels/v1/github  needs GITHUB_CONNECTOR + GITHUB_APP_SLUG

Inbound messages land in _EchoHub, which only logs them; the store and the
agent plug in behind channels.Hub later.
"""

import logging

import fastapi

import channels
import models
from channels import github, slack

log = logging.getLogger("app")

# hardcoded stubs until the store lands
STUB_PROJECTS = [
    models.Project(
        id="prj_self",
        name="fabricator",
        goal="work on itself: respond to issues, ship prs to its own repo",
        repos=["anbuzin/fabricator"],
        created_at="2026-08-10T09:00:00+00:00",
    ),
    models.Project(
        id="prj_wfjs",
        name="workflows watch",
        goal="monitor workflows js, notify python team on change",
        repos=["vercel/workflows"],
        created_at="2026-08-11T14:30:00+00:00",
    ),
]

STUB_CHATS = [
    models.Chat(
        id="chat_a1",
        project_id="prj_self",
        title="wire up the two-pane ui",
        trigger="ui",
        status="running",
        sandbox_id="sbx_9f2c",
        created_at="2026-08-13T10:05:00+00:00",
    ),
    models.Chat(
        id="chat_a2",
        project_id="prj_self",
        title="fix flaky slack channel test",
        trigger="slack:T024BE7LD",
        status="done",
        artifact="https://github.com/anbuzin/fabricator/pull/8",
        created_at="2026-08-12T16:40:00+00:00",
    ),
    models.Chat(
        id="chat_a3",
        project_id="prj_self",
        title="nightly repo sweep",
        trigger="cron",
        status="failed",
        artifact="sweep aborted: sandbox clone timed out",
        created_at="2026-08-13T03:00:00+00:00",
    ),
    models.Chat(
        id="chat_b1",
        project_id="prj_wfjs",
        title="weekly changelog digest",
        trigger="cron",
        status="queued",
        created_at="2026-08-13T08:00:00+00:00",
    ),
]


class _EchoHub:
    """Placeholder hub: logs inbound messages, dedupes in memory."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def dispatch(self, channel: str, inbound: channels.Inbound) -> None:
        log.info("inbound from %s token=%s: %s", channel, inbound.token, inbound.text)

    async def dedupe(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


bot = channels.App(_EchoHub())
bot.add(slack.channel())
bot.add(github.channel())

app = fastapi.FastAPI(title="fabricator")
app.include_router(bot.router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "channels": list(bot.channels)}


@app.get("/api/projects")
async def list_projects() -> list[models.Project]:
    return STUB_PROJECTS


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> models.Project:
    for project in STUB_PROJECTS:
        if project.id == project_id:
            return project
    raise fastapi.HTTPException(status_code=404, detail="unknown project")


@app.get("/api/projects/{project_id}/chats")
async def list_chats(project_id: str) -> list[models.Chat]:
    return [chat for chat in STUB_CHATS if chat.project_id == project_id]
