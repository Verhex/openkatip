"""Anthropic Messages adapter, with no local tool execution."""

from collections.abc import Mapping
from typing import Any

import anthropic

from katip_ops.config import ExecutorConfig, Prices
from katip_ops.core.contract import Cost, Task
from katip_ops.executors.base import (
    ExecutorResponse,
    Message,
    ProviderError,
    Tool,
    ToolCall,
    priced,
)


class ClaudeExecutor:
    provider = "claude"

    def __init__(
        self,
        name: str,
        config: ExecutorConfig,
        *,
        api_key: str = "",
        prices: Prices | None = None,
        model_prices: Mapping[str, Prices] | None = None,
        client: Any = None,
    ) -> None:
        self.name = name
        self.model = config.model
        self.config = config
        self.prices = prices
        self.model_prices = model_prices or {}
        self.client = (
            client
            if client is not None
            else anthropic.AsyncAnthropic(
                api_key=api_key,
                max_retries=config.max_retries,
                timeout=config.timeout_seconds,
                base_url=config.base_url,
            )
        )

    async def run(
        self,
        task: Task,
        system: str,
        messages: list[Message],
        tools: list[Tool],
    ) -> ExecutorResponse:
        # Reject caller-supplied server tools: only this adapter may enable web search.
        if any("type" in tool for tool in tools):
            raise ValueError("Only client tool schemas are accepted")
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        selected_tools = list(tools)
        if task.input.get("allow_web") is True:
            selected_tools.append({"name": "web_search", "type": self.config.web_search_type})
        if selected_tools:
            request["tools"] = selected_tools
        if self.config.thinking == "adaptive":
            request["thinking"] = {"type": "adaptive"}
            request["output_config"] = {"effort": self.config.effort}
        if self.config.server_fallback:
            request.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            create = self.client.beta.messages.create
        else:
            create = self.client.messages.create
        try:
            response = await create(**request)
        except anthropic.APIConnectionError:
            raise ProviderError("Anthropic connection failure after retries") from None
        except anthropic.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise ProviderError("Anthropic unavailable after retries") from None
            raise
        usage = response.usage
        server = usage.server_tool_use
        cost = priced(
            Cost(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                web_search_requests=server.web_search_requests if server else 0,
            ),
            self.prices if response.model == self.model else self.model_prices.get(response.model),
        )
        refused = response.stop_reason == "refusal"
        return ExecutorResponse(
            executor=self.name,
            model=response.model,
            text="".join(block.text for block in response.content if block.type == "text"),
            tool_calls=[]
            if refused
            else [
                ToolCall(id=block.id, name=block.name, arguments=block.input)
                for block in response.content
                if block.type == "tool_use"
            ],
            stop_reason=response.stop_reason,
            cost=cost,
            status="failed" if refused else "done",
            error="Model isteği reddetti." if refused else None,
        )
