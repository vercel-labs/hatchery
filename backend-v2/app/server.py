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
from channels import github, slack

log = logging.getLogger("app")


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
