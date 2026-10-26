"""Shared API-only executor boundary and explicit retryable provider failures."""

from typing import Literal, Protocol

from pydantic import Field, JsonValue

from katip_ops.config import Prices
from katip_ops.core.contract import Cost, FrozenModel, Task

Message = dict[str, JsonValue]
Tool = dict[str, JsonValue]


class ToolCall(FrozenModel):
    id: str
    name: str
    arguments: dict[str, JsonValue]


class ExecutorResponse(FrozenModel):
    executor: str | None = None
    model: str | None = None
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str | None = None
    cost: Cost = Field(default_factory=Cost)
    status: Literal["done", "failed"] = "done"
    error: str | None = None


class ProviderError(RuntimeError):
    """Retryable failure only: timeout, connection, 429 or 5xx after SDK retries."""


class Executor(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def run(
        self,
        task: Task,
        system: str,
        messages: list[Message],
        tools: list[Tool],
    ) -> ExecutorResponse: ...


def priced(cost: Cost, prices: Prices | None) -> Cost:
    if prices is None or any(value is None for value in prices.model_dump().values()):
        return cost
    assert prices.input is not None and prices.output is not None
    assert prices.cache_read is not None and prices.cache_write is not None
    assert prices.web_search is not None
    usd = (
        cost.input_tokens * prices.input
        + cost.output_tokens * prices.output
        + cost.cache_read_tokens * prices.cache_read
        + cost.cache_write_tokens * prices.cache_write
    ) / 1_000_000 + cost.web_search_requests * prices.web_search / 1000
    return cost.model_copy(update={"usd": usd})
