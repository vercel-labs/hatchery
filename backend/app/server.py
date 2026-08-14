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
STUB_SPACES = [
    models.Space(
        id="spc_self",
        name="fabricator",
        goal="work on itself: respond to issues, ship prs to its own repo",
        about=(
            "# fabricator\n\n"
            "An agent deployed to the cloud, running mostly unattended. Reachable "
            "from slack, github, and this ui.\n\n"
            "## Goal\n\n"
            "Work on itself: respond to issues, ping on slack, and ship prs to its "
            "own repo.\n\n"
            "## How it runs\n\n"
            "- two vercel services: fastapi backend, next.js frontend\n"
            "- chats spawn from slack, github, cron, or the ui\n"
            "- each chat gets a sandbox with the space's repos cloned\n\n"
            "## Conventions\n\n"
            "Keep changes small and reviewable. Prefer a report over a pr when "
            "uncertain."
        ),
        repos=["anbuzin/fabricator"],
        resources=[
            models.Resource(
                title="ai sdk for python",
                url="https://vercel.com/docs/ai-sdk-python",
                kind="reference",
            ),
            models.Resource(
                title="deployment",
                url="https://fabricator.vercel.app",
            ),
        ],
        color="#38bdf8",
        created_at="2026-08-10T09:00:00+00:00",
    ),
    models.Space(
        id="spc_wfjs",
        name="workflows watch",
        goal="monitor workflows js, notify python team on change",
        about=(
            "# workflows watch\n\n"
            "Monitor `vercel/workflows` and keep the python team in the loop.\n\n"
            "## What to watch\n\n"
            "- api changes in the js sdk that the python port should mirror\n"
            "- changelog entries and breaking releases\n\n"
            "## Output\n\n"
            "A short slack digest per notable change; an issue when the python "
            "port needs work."
        ),
        repos=["vercel/workflows"],
        resources=[
            models.Resource(
                title="workflows changelog",
                url="https://github.com/vercel/workflows/releases",
                kind="reference",
            ),
        ],
        color="#fbbf24",
        created_at="2026-08-11T14:30:00+00:00",
    ),
]

STUB_CHATS = [
    models.Chat(
        id="chat_a1",
        space_id="spc_self",
        title="wire up the two-pane ui",
        trigger="ui",
        status="running",
        sandbox_id="sbx_9f2c",
        created_at="2026-08-13T10:05:00+00:00",
    ),
    models.Chat(
        id="chat_a2",
        space_id="spc_self",
        title="fix flaky slack channel test",
        trigger="slack:T024BE7LD",
        status="done",
        artifact="https://github.com/anbuzin/fabricator/pull/8",
        created_at="2026-08-12T16:40:00+00:00",
    ),
    models.Chat(
        id="chat_a3",
        space_id="spc_self",
        title="nightly repo sweep",
        trigger="cron",
        status="failed",
        artifact="sweep aborted: sandbox clone timed out",
        created_at="2026-08-13T03:00:00+00:00",
    ),
    models.Chat(
        id="chat_b1",
        space_id="spc_wfjs",
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


@app.get("/api/spaces")
async def list_spaces() -> list[models.Space]:
    return STUB_SPACES


@app.get("/api/chats")
async def list_chats() -> list[models.Chat]:
    return STUB_CHATS
