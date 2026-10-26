"""Chat Completions for OpenAI and explicitly configured DeepSeek endpoints."""

import json
from collections.abc import Mapping
from typing import Any

import openai

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


class OpenAICompatExecutor:
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
        self.provider = config.provider
        self.model = config.model
        self.config = config
        self.prices = prices
        self.model_prices = model_prices or {}
        self.client = (
            client
            if client is not None
            else openai.AsyncOpenAI(
                api_key=api_key,
                base_url=config.base_url,
                max_retries=config.max_retries,
                timeout=config.timeout_seconds,
            )
        )

    async def run(
        self,
        task: Task,
        system: str,
        messages: list[Message],
        tools: list[Tool],
    ) -> ExecutorResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        # DeepSeek supports max_tokens; modern OpenAI models use max_completion_tokens.
        request["max_tokens" if self.provider == "deepseek" else "max_completion_tokens"] = (
            self.config.max_tokens
        )
        if tools:
            if any(tool.get("type") != "function" for tool in tools):
                raise ValueError("Only function tool schemas are accepted")
            request["tools"] = tools
        try:
            response = await self.client.chat.completions.create(**request)
        except openai.APIConnectionError:
            raise ProviderError("Chat provider connection failure after retries") from None
        except openai.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise ProviderError("Chat provider unavailable after retries") from None
            raise
        usage = response.usage
        cost = Cost()
        if usage is not None:
            details = usage.prompt_tokens_details
            # DeepSeek extensions are preserved as extra attributes by the SDK.
            read = (
                getattr(usage, "prompt_cache_hit_tokens", None)
                or (details.cached_tokens if details else 0)
                or 0
            )
            write = (details.cache_write_tokens if details else 0) or 0
            cost = priced(
                Cost(
                    input_tokens=max(0, usage.prompt_tokens - read - write),
                    output_tokens=usage.completion_tokens,
                    cache_read_tokens=read,
                    cache_write_tokens=write,
                ),
                self.prices
                if response.model == self.model
                else self.model_prices.get(response.model),
            )
        choice = response.choices[0]
        message = choice.message
        refused = bool(message.refusal) or choice.finish_reason == "content_filter"
        calls = []
        if not refused:
            for call in message.tool_calls or []:
                if call.type != "function":
                    raise ValueError("Unsupported chat tool response")
                calls.append(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=json.loads(call.function.arguments),
                    )
                )
        return ExecutorResponse(
            executor=self.name,
            model=response.model,
            text=message.content or "",
            tool_calls=calls,
            stop_reason=choice.finish_reason,
            cost=cost,
            status="failed" if refused else "done",
            error="Model isteği reddetti." if refused else None,
        )
