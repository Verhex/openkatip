"""Validated, installation-owned configuration. No credentials are loaded here."""

import os
import tomllib
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, field_validator, model_validator

from katip_ops.core.contract import DataClass, FrozenModel


class Prices(FrozenModel):
    input: float | None = Field(default=None, ge=0)
    output: float | None = Field(default=None, ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write: float | None = Field(default=None, ge=0)
    web_search: float | None = Field(default=None, ge=0)


class ExecutorConfig(FrozenModel):
    provider: Literal["claude", "openai", "deepseek", "xai"]
    model: str = Field(min_length=1)
    enabled: bool = False
    key_file: Path
    base_url: str | None = None
    allowed_data_classes: list[DataClass]
    max_tokens: int = Field(default=4096, gt=0)
    max_retries: int = Field(default=2, ge=0)
    timeout_seconds: float = Field(default=60, gt=0)
    thinking: Literal["adaptive", "off"] = "off"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    server_fallback: bool = False
    web_search_type: str = "web_search_20260209"

    @model_validator(mode="after")
    def validate_provider(self) -> Self:
        if "haiku" in self.model.lower():
            raise ValueError("Haiku models are not allowed")
        if self.provider == "deepseek":
            if set(self.allowed_data_classes) - {"public", "internal"}:
                raise ValueError("DeepSeek cannot receive personal or customer data")
            if not self.base_url:
                raise ValueError("DeepSeek requires an explicit base_url")
        return self


class Routing(FrozenModel):
    default: list[str] = Field(min_length=1)
    routes: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("routes")
    @classmethod
    def nonempty_routes(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if any(not route for route in value.values()):
            raise ValueError("Routes must contain at least one executor")
        return value


class ExecutorsConfig(FrozenModel):
    executors: dict[str, ExecutorConfig]
    routing: Routing
    prices: dict[str, Prices] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_routes(self) -> Self:
        for route in [self.routing.default, *self.routing.routes.values()]:
            if set(route) - self.executors.keys():
                raise ValueError("Route references an unknown executor")
        return self


class ApprovalRules(FrozenModel):
    stage: Literal[1, 2] = 1
    stage1: dict[str, bool] = Field(default_factory=dict)
    routine_kinds: list[str] = Field(default_factory=list)
    always_approval: list[str] = Field(default_factory=list)


class PolicySettings(FrozenModel):
    banned_terms: list[str] = Field(default_factory=list)
    customer_names: list[str] = Field(default_factory=list)
    personal_email_domains: list[str] = Field(default_factory=list)
    allowed_kinds: list[str] = Field(default_factory=list)
    approval_rules: ApprovalRules = Field(default_factory=ApprovalRules)


class OdooSettings(FrozenModel):
    base_url: str = "http://odoo-test:8069"
    db: str = "katip_agent_test"
    key_file: Path = Path("secrets/odoo_api_key_test")
    timeout_seconds: float = Field(default=30, gt=0)


class SlackSettings(FrozenModel):
    slack_bot_token: Path = Path("secrets/slack_bot_token")
    slack_app_token: Path = Path("secrets/slack_app_token")
    proxy: str | None = None
    channels: dict[Literal["ops", "ops_approvals", "pipeline", "social"], str] = Field(
        default_factory=dict
    )

    @field_validator("channels")
    @classmethod
    def channel_ids(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not channel.startswith(("C", "G")) or not channel.isalnum()
            for channel in value.values()
        ):
            raise ValueError("Slack channels must be conversation IDs")
        return value


class Schedule(FrozenModel):
    day_of_week: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    hour: int = Field(default=10, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    enabled: bool = True


class GitHubSettings(FrozenModel):
    org: str = ""
    token_file: Path = Path("secrets/github_token")


class GeoSettings(FrozenModel):
    mention_names: list[str] = Field(default_factory=list)
    citation_domains: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    assistants: list[str] = Field(default_factory=list)


JobName = Literal["pipeline", "icerik", "geo", "cuma"]


def default_schedules() -> dict[JobName, Schedule]:
    return {
        "pipeline": Schedule(day_of_week="tue"),
        "icerik": Schedule(day_of_week="wed"),
        "geo": Schedule(day_of_week="thu"),
        "cuma": Schedule(day_of_week="fri", hour=16),
    }


class Settings(FrozenModel):
    owner_slack_id: str = Field(min_length=1)
    database_path: Path = Path("state/ops.sqlite3")
    approval_ttl_hours: float = Field(default=72, gt=0)
    timezone: str = "Europe/Istanbul"
    ops_dir: Path = Path(".")
    dry_run: bool = True
    odoo: OdooSettings = Field(default_factory=OdooSettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    geo: GeoSettings = Field(default_factory=GeoSettings)
    content_excluded_repos: list[str] = Field(default_factory=list)
    max_tool_rounds: int = Field(default=3, ge=1, le=10)
    schedules: dict[JobName, Schedule] = Field(default_factory=default_schedules)

    @field_validator("timezone")
    @classmethod
    def istanbul_only(cls, value: str) -> str:
        if value != "Europe/Istanbul":
            raise ValueError("Schedules require Europe/Istanbul")
        return value


class Config(FrozenModel):
    settings: Settings
    executors: ExecutorsConfig


def load_config(config_dir: str | Path | None = None) -> Config:
    """KATIP_OPS_CONFIG names a directory; relative paths resolve inside it."""
    directory = Path(config_dir or os.getenv("KATIP_OPS_CONFIG") or "config").resolve()
    with (directory / "settings.toml").open("rb") as stream:
        settings = Settings.model_validate(tomllib.load(stream))
    with (directory / "executors.yaml").open(encoding="utf-8") as stream:
        executors = ExecutorsConfig.model_validate(yaml.safe_load(stream))
    settings = settings.model_copy(
        update={
            "database_path": directory / settings.database_path,
            "ops_dir": directory / settings.ops_dir,
            "github": settings.github.model_copy(
                update={"token_file": directory / settings.github.token_file}
            ),
            "odoo": settings.odoo.model_copy(
                update={"key_file": directory / settings.odoo.key_file}
            ),
            "slack": settings.slack.model_copy(
                update={
                    "slack_bot_token": directory / settings.slack.slack_bot_token,
                    "slack_app_token": directory / settings.slack.slack_app_token,
                }
            ),
        }
    )
    executors = executors.model_copy(
        update={
            "executors": {
                name: entry.model_copy(update={"key_file": directory / entry.key_file})
                for name, entry in executors.executors.items()
            }
        }
    )
    return Config(settings=settings, executors=executors)
