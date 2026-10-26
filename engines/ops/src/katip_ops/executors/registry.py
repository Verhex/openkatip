"""Build enabled adapters and report unavailable keys without exposing secrets."""

from dataclasses import dataclass, field

from katip_ops.config import ExecutorsConfig
from katip_ops.executors.base import Executor
from katip_ops.executors.claude import ClaudeExecutor
from katip_ops.executors.openai_compat import OpenAICompatExecutor
from katip_ops.secrets import SecretUnavailable, read_secret


@dataclass
class Registry:
    available: dict[str, Executor] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)


def build_executors(config: ExecutorsConfig) -> Registry:
    registry = Registry()
    for name, entry in config.executors.items():
        if not entry.enabled:
            continue
        if entry.provider == "xai":
            registry.unavailable[name] = "xAI yürütücüsü devre dışı."
            continue
        try:
            key = read_secret(entry.key_file)
        except SecretUnavailable:
            registry.unavailable[name] = f"Anahtar dosyası kullanılamıyor: {entry.key_file}"
            continue
        adapter = ClaudeExecutor if entry.provider == "claude" else OpenAICompatExecutor
        registry.available[name] = adapter(
            name,
            entry,
            api_key=key,
            prices=config.prices.get(entry.model),
            model_prices=config.prices,
        )
    return registry
