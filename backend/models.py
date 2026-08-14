"""Core entities: spaces and chats. (Named models.py: types.py would shadow stdlib types.)"""

import pydantic


class Space(pydantic.BaseModel):
    id: str  # "spc_<hex>"
    name: str
    goal: str  # "monitor workflows js, notify python team on change"
    repos: list[str] = []  # "owner/repo", autocloned into the sandbox
    color: str  # hex; the ui codes the space and its chats with it
    created_at: str  # utc isoformat, same as Event.meta.at


class Chat(pydantic.BaseModel):
    id: str  # "chat_<hex>"
    space_id: str
    title: str
    trigger: str  # what spawned it: "slack:<token>", "cron", "ui", ...
    status: str = "queued"  # queued | running | done | failed
    sandbox_id: str | None = None
    artifact: str | None = None  # report text or issue/pr url
    created_at: str
