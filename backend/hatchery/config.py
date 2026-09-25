"""Agent limits and review policies, read from the environment.

Ported from agentmesh `config.py` (`MeshConfig`) without `mesh.toml`. Every field is
the environment variable `HATCHERY_<SECTION>_<FIELD>`, for example
`HATCHERY_THREAD_MAX_TURNS`, `HATCHERY_REVIEW_WIKI`, or `HATCHERY_SERVE_DOMAIN`.
Unset variables keep agentmesh defaults; the model ID default is Hatchery's.
"""

import collections.abc
import os
import re
import typing

import pydantic

Policy = typing.Literal["auto", "review"]
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
MAX_COMMAND_TIMEOUT_SECONDS = 480


class ConfigError(ValueError):
    """Configuration cannot be used; the message names the offending variable."""


def validate_slug(value: str) -> str:
    """A portable lowercase name usable as a path component, Git ref segment, and DNS label."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ConfigError("name must be 1-63 lowercase letters, digits, or interior hyphens")
    return value


def validate_domain(value: str) -> str:
    normalized = value.removesuffix(".").lower()
    labels = normalized.split(".")
    if (
        not normalized
        or len(normalized) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise ConfigError("domain must be a valid DNS hostname without a port")
    return normalized


class _Strict(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", strict=True, frozen=True)


class ModelConfig(_Strict):
    id: str = "openai/gpt-5.6-sol"
    max_output_tokens: int = pydantic.Field(default=4096, gt=0)
    context_window_tokens: int | None = pydantic.Field(default=None, gt=0)

    @pydantic.field_validator("id")
    @classmethod
    def _provider_slash_model(cls, value: str) -> str:
        provider, _, model = value.partition("/")
        if not provider or not model or any(c.isspace() for c in value):
            raise ValueError("must be a provider/model identifier")
        return value

    @pydantic.model_validator(mode="after")
    def _output_fits_context(self) -> ModelConfig:
        if (
            self.context_window_tokens is not None
            and self.max_output_tokens >= self.context_window_tokens
        ):
            raise ValueError("max_output_tokens must be smaller than context_window_tokens")
        return self


class ThreadConfig(_Strict):
    max_turns: int = pydantic.Field(default=100, gt=0)
    bash_calls_per_turn: int = pydantic.Field(default=16, gt=0)
    command_timeout_seconds: int = pydantic.Field(
        default=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_COMMAND_TIMEOUT_SECONDS,
    )
    sandbox_idle_seconds: int = pydantic.Field(default=300, gt=0)
    compact_above_tokens: int = pydantic.Field(default=120_000, gt=0)
    keep_recent_messages: int = pydantic.Field(default=12, gt=0)
    max_delegation_depth: int = pydantic.Field(default=2, gt=0)
    max_delegations_per_thread: int = pydantic.Field(default=4, gt=0)


class BudgetConfig(_Strict):
    tokens_per_day: int = pydantic.Field(default=1_000_000, gt=0)


class ReviewConfig(_Strict):
    workspace: Policy = "auto"
    serve: Policy = "review"
    wiki: Policy = "review"


class ServeConfig(_Strict):
    domain: str = "localhost"
    revision_ttl_seconds: int = pydantic.Field(default=15, ge=0, le=300)
    request_timeout_seconds: int = pydantic.Field(default=30, gt=0, le=120)
    job_timeout_seconds: int = pydantic.Field(default=240, gt=0, le=240)
    max_request_bytes: int = pydantic.Field(default=1024 * 1024, gt=0, le=1024 * 1024)

    @pydantic.field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return validate_domain(value)


class Config(_Strict):
    model: ModelConfig = ModelConfig()
    thread: ThreadConfig = ThreadConfig()
    budget: BudgetConfig = BudgetConfig()
    review: ReviewConfig = ReviewConfig()
    serve: ServeConfig = ServeConfig()


def load(environ: collections.abc.Mapping[str, str] = os.environ) -> Config:
    """Read `HATCHERY_<SECTION>_<FIELD>` variables over agentmesh defaults."""
    data: dict[str, dict[str, str]] = {}
    for section, field in Config.model_fields.items():
        model = typing.cast(type[pydantic.BaseModel], field.annotation)
        for name in model.model_fields:
            value = environ.get(f"HATCHERY_{section}_{name}".upper())
            if value is not None:
                data.setdefault(section, {})[name] = value
    try:
        return Config.model_validate_strings(data)
    except pydantic.ValidationError as error:
        first = error.errors()[0]
        variable = "HATCHERY_" + "_".join(str(part) for part in first["loc"][:2]).upper()
        raise ConfigError(f"{variable}: {first['msg']}") from None
