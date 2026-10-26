import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anthropic
import httpx
import openai
import pytest
from anthropic.resources.beta.messages.messages import AsyncMessages as BetaMessages
from anthropic.resources.messages.messages import AsyncMessages
from anthropic.types import Message
from openai.resources.chat.completions.completions import AsyncCompletions
from openai.types.chat import ChatCompletion

from katip_ops.config import ExecutorsConfig, Routing
from katip_ops.executors.base import ProviderError
from katip_ops.executors.claude import ClaudeExecutor
from katip_ops.executors.openai_compat import OpenAICompatExecutor
from katip_ops.executors.registry import build_executors
from katip_ops.executors.xai import XAIExecutor


def claude_response(stop="end_turn"):
    return Message.model_validate(
        {
            "id": "msg_fake",
            "model": "configured-model",
            "role": "assistant",
            "type": "message",
            "content": [
                {"type": "text", "text": "Taslak hazır."},
                {"type": "tool_use", "id": "tool1", "name": "create_lead", "input": {"a": 1}},
            ],
            "stop_reason": stop,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 10,
                "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 0},
            },
        }
    )


def chat_response(refused=False, usage=True, deepseek=False):
    payload = {
        "id": "chat_fake",
        "model": "configured-model",
        "created": 1,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Taslak hazır.",
                    "refusal": "Reddedildi." if refused else None,
                    "tool_calls": [
                        {
                            "id": "tool1",
                            "type": "function",
                            "function": {
                                "name": "create_lead",
                                "arguments": '{"a":1}',
                            },
                        }
                    ],
                },
            }
        ],
    }
    if usage:
        payload["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
        }
        if deepseek:
            payload["usage"]["prompt_cache_hit_tokens"] = 30
    return ChatCompletion.model_validate(payload)


def fake_client(response):
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(
        messages=SimpleNamespace(create=create),
        beta=SimpleNamespace(messages=SimpleNamespace(create=create)),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    return client, create


@pytest.mark.parametrize("beta", [False, True])
async def test_claude_mapping_request_and_pricing(task, executor_config, prices, beta):
    client, create = fake_client(claude_response())
    config = executor_config.model_copy(update={"thinking": "adaptive", "server_fallback": beta})
    executor = ClaudeExecutor("claude", config, client=client, prices=prices)
    response = await executor.run(
        task.model_copy(update={"input": {"allow_web": True}}),
        "Stable instructions",
        [{"role": "user", "content": "Draft"}],
        [],
    )
    assert response.text == "Taslak hazır."
    assert response.tool_calls[0].arguments == {"a": 1}
    assert response.cost.input_tokens == 100
    assert response.cost.cache_read_tokens == 20
    assert response.cost.cache_write_tokens == 10
    assert response.cost.web_search_requests == 2
    assert response.cost.usd == pytest.approx(0.01045)
    request = create.call_args.kwargs
    inspect.signature((BetaMessages if beta else AsyncMessages).create).bind(None, **request)
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "high"}
    assert request["tools"] == [{"name": "web_search", "type": "web_search_20260209"}]
    assert not {"temperature", "top_p", "top_k"}.intersection(request)
    if beta:
        assert request["betas"] == ["server-side-fallback-2026-07-01"]
        assert request["fallbacks"] == "default"


@pytest.mark.parametrize("allow_web", [None, False, 1, "true"])
async def test_claude_web_requires_literal_true(task, executor_config, allow_web):
    client, create = fake_client(claude_response("refusal"))
    executor = ClaudeExecutor("claude", executor_config, client=client)
    response = await executor.run(
        task.model_copy(update={"input": {"allow_web": allow_web}}), "system", [], []
    )
    assert response.status == "failed"
    assert response.tool_calls == []
    assert response.cost.usd is None
    assert "tools" not in create.call_args.kwargs
    assert "thinking" not in create.call_args.kwargs
    with pytest.raises(ValueError):
        await executor.run(task, "", [], [{"type": "web_search_20260209", "name": "web_search"}])


async def test_optional_anthropic_usage_fields(task, executor_config):
    response = claude_response()
    response.usage.cache_read_input_tokens = None
    response.usage.cache_creation_input_tokens = None
    response.usage.server_tool_use = None
    client, _ = fake_client(response)
    result = await ClaudeExecutor("c", executor_config, client=client).run(task, "", [], [])
    assert result.cost.cache_read_tokens == result.cost.web_search_requests == 0


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_chat_mapping_and_pricing(task, executor_config, prices, provider):
    client, create = fake_client(chat_response(deepseek=provider == "deepseek"))
    config = executor_config.model_copy(update={"provider": provider})
    response = await OpenAICompatExecutor("chat", config, client=client, prices=prices).run(
        task,
        "stable",
        [{"role": "user", "content": "Draft"}],
        [],
    )
    assert response.text == "Taslak hazır."
    assert response.cost.input_tokens == (60 if provider == "deepseek" else 70)
    assert response.cost.output_tokens == 50
    assert response.cost.cache_read_tokens == (30 if provider == "deepseek" else 20)
    assert response.cost.cache_write_tokens == 10
    assert response.cost.usd == pytest.approx(0.00038 if provider == "deepseek" else 0.00039)
    assert response.tool_calls[0].name == "create_lead"
    assert response.tool_calls[0].arguments == {"a": 1}
    request = create.call_args.kwargs
    inspect.signature(AsyncCompletions.create).bind(None, **request)
    assert request["messages"][0] == {"role": "system", "content": "stable"}
    assert ("max_tokens" if provider == "deepseek" else "max_completion_tokens") in request


@pytest.mark.parametrize("missing_usage", [False, True])
async def test_chat_refusal_unknown_price(task, executor_config, prices, missing_usage):
    client, _ = fake_client(chat_response(refused=True, usage=not missing_usage))
    config = executor_config.model_copy(update={"provider": "openai"})
    result = await OpenAICompatExecutor(
        "o", config, client=client, prices=prices.model_copy(update={"cache_write": None})
    ).run(
        task,
        "",
        [],
        [],
    )
    assert result.status == "failed"
    assert result.tool_calls == []
    assert result.cost.usd is None


@pytest.mark.parametrize(
    "adapter,sdk", [(ClaudeExecutor, anthropic), (OpenAICompatExecutor, openai)]
)
@pytest.mark.parametrize("status", [429, 500, 503, 400, 401])
async def test_provider_status_errors(task, executor_config, adapter, sdk, status):
    client, create = fake_client(None)
    request = httpx.Request("POST", "https://example.invalid")
    error = sdk.APIStatusError("failure", response=httpx.Response(status, request=request), body={})
    create.side_effect = error
    with pytest.raises(ProviderError if status in [429, 500, 503] else sdk.APIStatusError):
        await adapter("fake", executor_config, client=client).run(task, "", [], [])


@pytest.mark.parametrize(
    "adapter,sdk", [(ClaudeExecutor, anthropic), (OpenAICompatExecutor, openai)]
)
async def test_provider_timeout(task, executor_config, adapter, sdk):
    client, create = fake_client(None)
    create.side_effect = sdk.APITimeoutError(
        request=httpx.Request("POST", "https://example.invalid")
    )
    with pytest.raises(ProviderError):
        await adapter("fake", executor_config, client=client).run(task, "", [], [])


def test_registry_missing_keys_disabled_and_xai(executor_config, tmp_path, monkeypatch):
    calls = []

    def factory(name, config, **kwargs):
        calls.append((name, kwargs["api_key"]))
        return SimpleNamespace(name=name)

    monkeypatch.setattr("katip_ops.executors.registry.ClaudeExecutor", factory)
    monkeypatch.setattr("katip_ops.executors.registry.OpenAICompatExecutor", factory)
    key = tmp_path / "key"
    key.write_text("fake-test-key\n")
    entries = {
        "claude": executor_config.model_copy(update={"key_file": key}),
        "missing": executor_config.model_copy(update={"key_file": tmp_path / "missing"}),
        "disabled": executor_config.model_copy(update={"enabled": False}),
        "openai": executor_config.model_copy(update={"provider": "openai", "key_file": key}),
        "xai": executor_config.model_copy(update={"provider": "xai"}),
    }
    registry = build_executors(
        ExecutorsConfig(executors=entries, routing=Routing(default=["claude"]))
    )
    assert set(registry.available) == {"claude", "openai"}
    assert set(registry.unavailable) == {"missing", "xai"}
    assert calls == [("claude", "fake-test-key"), ("openai", "fake-test-key")]


async def test_disabled_xai(task):
    with pytest.raises(NotImplementedError):
        await XAIExecutor().run(task, "", [], [])


@pytest.mark.parametrize("known_price", [False, True])
@pytest.mark.parametrize("provider", ["claude", "openai"])
async def test_returned_model_uses_its_own_price(
    task,
    executor_config,
    prices,
    known_price,
    provider,
):
    raw = claude_response() if provider == "claude" else chat_response()
    raw.model = "substitute-model"
    client, _ = fake_client(raw)
    adapter = ClaudeExecutor if provider == "claude" else OpenAICompatExecutor
    actual_prices = {"substitute-model": prices} if known_price else {}
    result = await adapter(
        "adapter",
        executor_config,
        client=client,
        prices=prices,
        model_prices=actual_prices,
    ).run(task, "", [], [])
    assert result.executor == "adapter"
    assert result.model == "substitute-model"
    assert (result.cost.usd is not None) == known_price


async def test_provider_native_tools_are_forwarded_without_execution(task, executor_config):
    claude_client, claude_create = fake_client(claude_response())
    tool = {"name": "proposal", "input_schema": {"type": "object"}}
    tools = [tool]
    result = await ClaudeExecutor("c", executor_config, client=claude_client).run(
        task,
        "",
        [{"role": "user", "content": "Draft"}],
        tools,
    )
    assert result.tool_calls
    assert tools == [tool]
    assert claude_create.call_args.kwargs["tools"] == tools
    chat_client, chat_create = fake_client(chat_response())
    tool = {"type": "function", "function": {"name": "proposal", "parameters": {"type": "object"}}}
    adapter = OpenAICompatExecutor("o", executor_config, client=chat_client)
    await adapter.run(task, "", [], [tool])
    assert chat_create.call_args.kwargs["tools"] == [tool]
    with pytest.raises(ValueError):
        await adapter.run(task, "", [], [{"type": "web_search"}])


def test_client_construction_passes_configured_options(executor_config, monkeypatch):
    # Constructors are synchronous; their return values only need to be retained.
    anthropic_constructor = Mock()
    chat_constructor = Mock()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", anthropic_constructor)
    monkeypatch.setattr(openai, "AsyncOpenAI", chat_constructor)
    config = executor_config.model_copy(update={"base_url": "https://example.invalid/v1"})
    ClaudeExecutor("c", config, api_key="fake-key")
    OpenAICompatExecutor("o", config, api_key="fake-key")
    for constructor in [anthropic_constructor, chat_constructor]:
        assert constructor.call_args.kwargs == {
            "api_key": "fake-key",
            "base_url": "https://example.invalid/v1",
            "max_retries": 2,
            "timeout": 60,
        }


async def test_chat_absent_cache_details_and_content_filter(task, executor_config, prices):
    raw = chat_response()
    raw.usage.prompt_tokens_details = None
    raw.choices[0].finish_reason = "content_filter"
    client, _ = fake_client(raw)
    result = await OpenAICompatExecutor("o", executor_config, client=client, prices=prices).run(
        task,
        "",
        [],
        [],
    )
    assert result.status == "failed" and result.tool_calls == []
    assert result.cost.input_tokens == 100
    assert result.cost.cache_read_tokens == 0
    assert result.cost.usd == pytest.approx(0.0004)
