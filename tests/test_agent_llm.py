"""Canlı model ayarları ve kullanım kaydı (Claude sahte)."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from katip.agent import llm
from katip.config import get_settings


@pytest.mark.parametrize(
    "model,supported,thinking",
    [
        ("claude-haiku-4-5", False, {}),
        ("claude-3-haiku-20240307", False, {}),
        ("claude-3-5-sonnet-20241022", False, {"thinking": {"type": "disabled"}}),
        ("claude-haiku-5-5", True, {"thinking": {"type": "disabled"}}),
        ("claude-haiku-5-6", True, {"thinking": {"type": "disabled"}}),
        ("claude-sonnet-5-5", True, {"thinking": {"type": "between_tools"}}),
        ("claude-opus-5-5", True, {"thinking": {"type": "between_tools"}}),
    ],
)
def test_model_capabilities(model, supported, thinking):
    assert llm.supports_effort(model) is supported
    assert llm.thinking_off(model) == thinking
    settings = NS(agent_model=model, agent_effort="low", agent_thinking=False)
    assert llm._effort(settings) == (
        {"output_config": {"effort": "low"}, **thinking} if supported else {}
    )
    settings.agent_thinking = True
    assert llm._effort(settings) == ({"output_config": {"effort": "low"}} if supported else {})


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_haiku_effort_with_thinking_disabled(effort):
    settings = NS(agent_model="claude-haiku-5-5", agent_effort=effort, agent_thinking=False)
    assert llm._effort(settings) == {
        "output_config": {"effort": effort},
        "thinking": {"type": "disabled"},
    }


def test_default_models(monkeypatch):
    monkeypatch.delenv("KATIP_AGENT_MODEL", raising=False)
    monkeypatch.delenv("KATIP_LLM_MODEL", raising=False)
    settings = get_settings()
    assert settings.agent_model == "claude-sonnet-5-5"
    assert settings.llm_model == "claude-sonnet-5-5"
    assert settings.agent_thinking is True


@pytest.mark.parametrize("refusal", [False, True])
async def test_ask_records_usage_once(monkeypatch, refusal):
    monkeypatch.setenv("KATIP_AGENT_MODEL", "claude-haiku-5-5")
    response = NS(
        usage=NS(input_tokens=10, output_tokens=5),
        stop_reason="refusal" if refusal else "end_turn",
        content=[NS(type="thinking", thinking="Gizli"), NS(type="text", text=" Cevap. ")],
    )
    create = AsyncMock(return_value=response)
    record = AsyncMock()
    monkeypatch.setattr(llm.usage, "record_llm", record)
    result = await llm._ask("Sistem", "Soru", NS(messages=NS(create=create)))
    assert result == ("Bu isteğe yardımcı olamıyorum." if refusal else "Cevap.")
    record.assert_awaited_once_with("soru", "claude-haiku-5-5", response.usage)
    assert create.call_args.kwargs["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("refusal", [False, True])
async def test_describe_image_records_usage_once_and_handles_refusal(monkeypatch, refusal):
    monkeypatch.setenv("KATIP_AGENT_MODEL", "claude-haiku-5-5")
    response = NS(
        usage=NS(input_tokens=10, output_tokens=5),
        stop_reason="refusal" if refusal else "end_turn",
        content=[NS(type="thinking", thinking="Gizli"), NS(type="text", text=" Grafik. ")],
    )
    client = NS(messages=NS(create=AsyncMock(return_value=response)))
    record = AsyncMock()
    monkeypatch.setattr(llm.usage, "record_llm", record)
    assert await llm.describe_image(b"jpeg", client=client) == ("" if refusal else "Grafik.")
    record.assert_awaited_once_with("görsel", "claude-haiku-5-5", response.usage)
