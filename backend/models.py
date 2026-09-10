"""Core entities: spaces and chats. (Named models.py: types.py would shadow stdlib types.)"""

import typing

import pydantic


AttentionReason = typing.Literal["result_available", "blocked"]
AccentColor = typing.Literal[
    "blue-600",
    "blue-700",
    "blue-800",
    "blue-900",
    "red-600",
    "red-700",
    "red-800",
    "red-900",
    "amber-600",
    "amber-700",
    "amber-800",
    "amber-900",
    "green-600",
    "green-700",
    "green-800",
    "green-900",
    "teal-600",
    "teal-700",
    "teal-800",
    "teal-900",
    "purple-600",
    "purple-700",
    "purple-800",
    "purple-900",
    "pink-600",
    "pink-700",
    "pink-800",
    "pink-900",
]


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
    color: str  # semantic accent ID; legacy aliases/custom values remain readable
    created_at: str  # utc isoformat, same as Event.meta.at

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise ValueError("repos must use owner/repo form")
        return repos


class NoteSummary(pydantic.BaseModel):
    filename: str
    revision: int
    updated_at: str


class Note(NoteSummary):
    space_id: str
    content: str


class Job(pydantic.BaseModel):
    id: str
    space_id: str
    owner_id: str
    author_display_name: str | None = None
    schedule: str
    prompt: str
    paused: bool = False
    next_run_at: str
    created_at: str


class Chat(pydantic.BaseModel):
    id: str  # "chat_<hex>"
    user_id: str | None = None
    author_display_name: str | None = None
    space_id: str | None = None
    title: str
    topic: str | None = None
    trigger: str  # what spawned it: "slack:<token>", "cron", "ui", ...
    status: str = "queued"  # queued | running | done | failed
    sandbox_id: str | None = None
    artifact: str | None = None  # report text or issue/pr url
    attention_reason: AttentionReason | None = None
    archived_at: str | None = None
    telemetry_span: dict[str, typing.Any] | None = None
    created_at: str
