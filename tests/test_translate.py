"""Canlı çeviri: sahte Claude akışı, model uyumluluğu ve kullanım kaydı."""

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from katip.pipeline import translate


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-haiku-5-5", "claude-sonnet-5-5"])
@pytest.mark.parametrize("final_usage", [False, True])
async def test_translation_stream_uses_live_model_and_supported_effort(
    monkeypatch, model, final_usage
):
    monkeypatch.setenv("KATIP_AGENT_MODEL", model)
    monkeypatch.setenv("KATIP_LLM_MODEL", "claude-opus-5-5")
    seen = {}
    tokens = NS(input_tokens=10, output_tokens=5)
    record = AsyncMock()
    monkeypatch.setattr(translate.usage, "record_llm", record)

    async def pieces():
        yield "Hello "
        yield "world"

    stream = NS(text_stream=pieces())
    if final_usage:
        stream.get_final_message = AsyncMock(return_value=NS(usage=tokens))

    @asynccontextmanager
    async def streaming(**kwargs):
        seen.update(kwargs)
        yield stream

    monkeypatch.setattr(
        translate, "AsyncAnthropic", lambda **kwargs: NS(messages=NS(stream=streaming))
    )
    translator = translate.Translator()
    chunks = translator.translate_stream("Merhaba dünya")
    assert await anext(chunks) == "Hello "
    record.assert_not_awaited()
    assert await anext(chunks) == "world"
    record.assert_not_awaited()
    with pytest.raises(StopAsyncIteration):
        await anext(chunks)

    assert seen["model"] == model
    assert seen["messages"][-1]["role"] == "user"
    if model == "claude-haiku-4-5":
        assert "output_config" not in seen and "thinking" not in seen
    else:
        assert seen["output_config"] == {"effort": "low"}
        assert seen["thinking"] == {
            "type": "disabled" if model == "claude-haiku-5-5" else "between_tools"
        }
    if final_usage:
        stream.get_final_message.assert_awaited_once_with()
        record.assert_awaited_once_with("çeviri", model, tokens)
    else:
        record.assert_not_awaited()
    assert translator._history[-1].target == "Hello world"
