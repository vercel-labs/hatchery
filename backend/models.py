"""Core entities: spaces and chats. (Named models.py: types.py would shadow stdlib types.)"""

import pydantic


class Resource(pydantic.BaseModel):
    title: str
    url: str
    kind: str = "link"  # link | reference | ...


class Space(pydantic.BaseModel):
    id: str  # "spc_<hex>"
    name: str
    about: str = ""  # markdown, the space's canvas
    repos: list[str] = []  # "owner/repo", autocloned into the sandbox
    resources: list[Resource] = []  # extra links; repos show up alongside these
    color: str  # hex; the ui codes the space and its chats with it
    created_at: str  # utc isoformat, same as Event.meta.at

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise ValueError("repos must use owner/repo form")
        return repos


class Chat(pydantic.BaseModel):
    id: str  # "chat_<hex>"
    user_id: str | None = None
    space_id: str | None = None
    title: str
    topic: str | None = None
    trigger: str  # what spawned it: "slack:<token>", "cron", "ui", ...
    status: str = "queued"  # queued | running | done | failed
    sandbox_id: str | None = None
    artifact: str | None = None  # report text or issue/pr url
    created_at: str
