"""Version-one task envelope and canonical action identity."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

DataClass = Literal["public", "internal", "personal", "customer"]
Status = Literal["done", "needs_approval", "failed", "rejected_by_policy"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Task(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    source: Literal["schedule", "slack", "katip", "manual"]
    requested_by: str
    sop: str | None = None
    input: dict[str, JsonValue] = Field(default_factory=dict)
    requires_approval: bool = True
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    data_class: DataClass = "internal"


class ProposedAction(FrozenModel):
    kind: str
    target: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str
    requires_approval: bool = True

    @property
    def digest(self) -> str:
        # Recompute: frozen models still contain mutable JSON dictionaries.
        canonical = json.dumps(
            {"kind": self.kind, "target": self.target, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Cost(FrozenModel):
    """Input tokens exclude cache reads/writes, which are billed separately."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    web_search_requests: int = Field(default=0, ge=0)
    usd: float | None = Field(default=None, ge=0)


class TaskResult(FrozenModel):
    task_id: str
    executor: str
    status: Status
    output: dict[str, JsonValue] = Field(default_factory=dict)
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    claims_used: list[str] = Field(default_factory=list)
    cost: Cost = Field(default_factory=Cost)
    run_id: str


class Event(FrozenModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: Literal["meeting.summary_ready", "mail.received", "task.assign"]
    source: str
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, JsonValue] = Field(default_factory=dict)
