"""Model fiyatları, uzun istem eşiği ve önbellek maliyetleri."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from katip import usage


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "read,expected",
    [
        (29_999, 0.01504999),
        (30_000, 0.01505),
        (30_001, 0.07525005),
    ],
)
def test_haiku_short_and_long_prompts_with_cache_and_batch(read, expected, batch):
    tokens = NS(
        input_tokens=20_000,
        output_tokens=10_000,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=50_000,
        cache_creation=NS(ephemeral_1h_input_tokens=20_000),
    )
    assert usage.llm_cost("claude-haiku-5-5", tokens, batch=batch) == pytest.approx(
        expected * (0.5 if batch else 1)
    )


@pytest.mark.parametrize("input_tokens,expected", [(100_000, 0.06), (100_001, 0.3000005)])
def test_haiku_prompt_tier_excludes_output_tokens(input_tokens, expected):
    tokens = NS(input_tokens=input_tokens, output_tokens=100_000)
    assert usage.llm_cost("claude-haiku-5-5", tokens) == pytest.approx(expected)


def test_opus_price_with_cache():
    tokens = NS(
        input_tokens=1_000,
        output_tokens=100,
        cache_read_input_tokens=300,
        cache_creation_input_tokens=200,
        cache_creation=NS(ephemeral_1h_input_tokens=100),
    )
    assert usage.llm_cost("claude-opus-5-5", tokens) == pytest.approx(0.00736)


def test_unknown_model_warns_once_per_id_and_uses_default_price(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(usage.log, "warning", warning)
    monkeypatch.setattr(usage, "_warned_models", set())
    tokens = NS(input_tokens=1_000, output_tokens=100)
    for model in ("unknown-a", "unknown-a", "unknown-b", "unknown-b"):
        assert usage.llm_cost(model, tokens) == pytest.approx(0.003)
    assert warning.call_count == 2
    assert [call.kwargs["model"] for call in warning.call_args_list] == ["unknown-a", "unknown-b"]
