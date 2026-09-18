"""Core entities: agents and threads. (models.py avoids shadowing stdlib types.)"""

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
    kind: str = "link"


class Agent(pydantic.BaseModel):
    id: str  # "agt_<hex>"
    slug: str
    name: str
    about: str = ""  # migration fallback; AGENTS.md is authoritative when configured
    repos: list[str] = []
    resources: list[Resource] = []
    color: str
    created_at: str

    @pydantic.field_validator("slug")
    @classmethod
    def valid_slug(cls, slug: str) -> str:
        if (
            not slug
            or len(slug) > 63
            or not slug.isascii()
            or slug != slug.lower()
            or slug[0] == "-"
            or slug[-1] == "-"
            or any(not (character.isalnum() or character == "-") for character in slug)
            or "--" in slug
        ):
            raise ValueError("slug must be lowercase ASCII letters, numbers, and single dashes")
        return slug

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise ValueError("repos must use owner/repo form")
        return repos


class AppSettings(pydantic.BaseModel):
    memory_repository: str | None = None
    memory_repository_installation_id: str | None = None

    @pydantic.field_validator("memory_repository")
    @classmethod
    def valid_memory_repository(cls, repository: str | None) -> str | None:
        if repository is None:
            return None
        parts = repository.split("/")
        if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
            raise ValueError("memory_repository must use owner/repo form")
        return repository


class AgentFilesSnapshot(pydantic.BaseModel):
    agent_slug: str
    revision: str | None
    files: list[str]


class AgentFile(pydantic.BaseModel):
    agent_slug: str
    path: str
    content: str
    revision: str


class Job(pydantic.BaseModel):
    id: str
    agent_id: str
    owner_id: str
    name: str | None = None
    author_display_name: str | None = None
    schedule: str
    schedule_kind: typing.Literal["cron", "every"] = "cron"
    timezone: str | None = None
    source_digest: str | None = None
    prompt: str
    paused: bool = False
    next_run_at: str
    created_at: str


class Thread(pydantic.BaseModel):
    id: str
    user_id: str | None = None
    author_display_name: str | None = None
    agent_id: str | None = None
    title: str
    topic: str | None = None
    trigger: str
    status: str = "queued"
    sandbox_id: str | None = None
    artifact: str | None = None
    attention_reason: AttentionReason | None = None
    archived_at: str | None = None
    telemetry_span: dict[str, typing.Any] | None = None
    created_at: str
