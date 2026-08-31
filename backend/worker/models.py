"""Stable app-side worker and task models."""

import typing

import pydantic


class WorkerSpec(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    title: str = "worker"
    repos: list[str] = []
    setup_script: str | None = None
    ports: list[int] = []
    branch: str | None = None
    git_sha: str | None = None
    vcpus: int | None = None
    memory: int | None = None

    @pydantic.field_validator("title")
    @classmethod
    def valid_title(cls, title: str) -> str:
        return title.strip()[:80] or "worker"

    @pydantic.field_validator("repos")
    @classmethod
    def valid_repos(cls, repos: list[str]) -> list[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise ValueError("repos must use owner/repo form")
        return repos

    @pydantic.field_validator("ports")
    @classmethod
    def valid_ports(cls, ports: list[int]) -> list[int]:
        if len(ports) > 4 or any(port < 1 or port > 65535 for port in ports):
            raise ValueError("ports must contain up to four values between 1 and 65535")
        return ports

    @pydantic.model_validator(mode="after")
    def normalize(self):
        if (self.branch or self.git_sha) and not self.repos:
            raise ValueError("branch and git_sha require a main repo")
        self.setup_script = self.setup_script.strip() if self.setup_script else None
        self.branch = self.branch.strip() if self.branch else None
        self.git_sha = self.git_sha.strip() if self.git_sha else None
        return self


class Route(pydantic.BaseModel):
    port: int
    url: str


class Worker(pydantic.BaseModel):
    id: str
    chat_id: str
    sandbox_name: str
    command_topic: str
    title: str
    status: typing.Literal["creating", "running", "stopped", "failed"]
    spec: WorkerSpec
    routes: list[Route] = []
    daemon_token: str
    daemon_version: int | None = None
    created_at: str
    updated_at: str


class Terminal(pydantic.BaseModel):
    id: str
    chat_id: str
    worker_id: str
    title: str
    status: typing.Literal["creating", "running", "exited"] = "creating"
    created_at: str
    updated_at: str


class TaskInput(pydantic.BaseModel):
    id: str
    sequence: int
    text: str
    created_at: str
    delivered_at: str | None = None


class Task(pydantic.BaseModel):
    id: str
    chat_id: str
    worker_id: str
    title: str
    prompt: str
    model: str
    status: typing.Literal[
        "pending", "running", "attention", "complete", "errored", "cancelled"
    ] = "pending"
    command_sequence: int = 0
    event_sequence: int = -1
    event_ids: list[str] = []
    source_sequences: dict[str, int] = {}
    inputs: list[TaskInput] = []
    active_question: str | None = None
    active_question_id: str | None = None
    open_tool_calls: list[str] = []
    pull_requests: list[dict[str, str]] = []
    last_agent_event_at: str | None = None
    last_agent_words: str | None = None
    transcript_event_count: int = 0
    transcript_tool_call_count: int = 0
    transcript_truncated_count: int = 0
    fx_session_id: str | None = None
    launch_attempts: int = 0
    result: dict[str, typing.Any] | None = None
    telemetry_span: dict[str, typing.Any] | None = None
    completion_delivered: bool = False
    created_at: str
    updated_at: str
