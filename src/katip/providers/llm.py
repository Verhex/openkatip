"""Claude adaptörü: taşıma farkları ve rapor yetenek kaybı burada karşılanır."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ValidationError

from katip import usage
from katip.providers.interfaces import Capabilities

_SERVER_FALLBACK = ("claude-opus-5", "claude-fable", "claude-mythos", "claude-sonnet-5-5")


def server_fallback(model: str) -> dict:
    """Sunucu tarafı ret yedeği (yalnız Claude API): reddedilen istek uygun modele düşer.

    Sonnet 5.5 bunu kabul ediyor (canlı API, 24 Ekim 2026); orada yalnız cyber ve
    frontier_llm retleri yeniden denenir. Bedrock/Vertex'te `params` alanları siler.
    """
    if model.startswith(_SERVER_FALLBACK):
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


class ClaudeProvider:
    def __init__(
        self,
        name: str,
        region: str,
        client: Any,
        capabilities: Capabilities,
        *,
        model_prefix: str = "eu.anthropic.",
        model_override: str | None = None,
    ):
        self.name = name
        self.region = region
        self.client = client
        self.capabilities = capabilities
        self.model_prefix = model_prefix
        self.model_override = model_override

    def params(self, params: dict) -> dict:
        args = deepcopy(params)
        model = args["model"]
        if self.name == "bedrock":
            args["model"] = self.model_override or f"{self.model_prefix}{model}"
        if self.name != "anthropic":
            args.pop("betas", None)
            args.pop("fallbacks", None)
        if not self.capabilities.output_config:
            args.pop("output_config", None)
        if not self.capabilities.thinking_config:
            # Bulut desteği doğrulanmadı: modelin varsayılanını kullan, disabled dayatma.
            args.pop("thinking", None)
        if not self.capabilities.thinking_signatures:
            stripped = False
            # Düşünme imzaları hesaplar arasında taşınamaz.
            for message in args.get("messages", []):
                if isinstance(message.get("content"), list):
                    content = []
                    for block in message["content"]:
                        kind = block.get("type") if isinstance(block, dict) else block.type
                        if kind in ("thinking", "redacted_thinking"):
                            stripped = True
                        else:
                            content.append(block)
                    message["content"] = content
            if stripped:
                args.pop("thinking", None)
                # Zorunlu düşünmeli modellerde imzasız eski araç turu 400 verebilir.
                # Araç çağrısı ve sonucu metin geçmişi olur; yeni araçlar kullanılabilir.
                for message in args.get("messages", []):
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for index, block in enumerate(content):
                        value = block if isinstance(block, dict) else block.model_dump()
                        if value.get("type") in ("tool_use", "tool_result"):
                            content[index] = {
                                "type": "text",
                                "text": "Önceki araç işlemi: "
                                + json.dumps(value, ensure_ascii=False),
                            }
                    if not content:
                        message["content"] = "Önceki düşünme içeriği aktarılmadı."
        if not self.capabilities.prompt_cache_1h:

            def strip(value):
                if isinstance(value, dict):
                    value.pop("cache_control", None)
                    for child in value.values():
                        strip(child)
                elif isinstance(value, list):
                    for child in value:
                        strip(child)

            strip(args)
        return args

    def annotate(self, response: Any, model: str) -> Any:
        usage.annotate(getattr(response, "usage", None), self.name, model, self.region)
        return response

    async def create(self, **params: Any) -> Any:
        args = self.params(params)
        if self.name == "anthropic" and "betas" not in args:
            args.update(server_fallback(args["model"]))
        # Beta alanları (ret yedeği) yalnızca beta ucunda kabul edilir.
        api = self.client.beta.messages if "betas" in args else self.client.messages
        response = await api.create(**args)
        return self.annotate(response, params["model"])

    async def stream(self, **params: Any) -> AsyncIterator[str]:
        async with self.client.messages.stream(**self.params(params)) as stream:
            async for piece in stream.text_stream:
                yield piece

    async def structured(
        self, schema: type[BaseModel], *, repair_budget: list[int] | None = None, **params: Any
    ) -> BaseModel:
        repair_budget = repair_budget if repair_budget is not None else [1]
        if self.capabilities.structured_output:
            response = await self.client.beta.messages.parse(
                **self.params(params), output_format=schema
            )
            self.annotate(response, params["model"])
            await usage.record_llm("rapor", params["model"], response.usage)
            if response.stop_reason == "refusal" or response.parsed_output is None:
                raise RuntimeError("Rapor üretilemedi")
            return response.parsed_output
        # Bedrock 5.5 structured output sunmaz. Şema hatası sağlayıcı değiştirmez;
        # aynı sağlayıcıda tek onarım, aynı toplam çağrı süresi içinde yapılır.
        args = deepcopy(params)
        args["messages"].append(
            {
                "role": "user",
                "content": "Yalnızca bu şemaya uygun JSON döndür:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False),
            }
        )
        for attempt in range(2):
            response = await self.create(**args)
            await usage.record_llm("rapor", params["model"], response.usage)
            if response.stop_reason == "refusal":
                raise RuntimeError("Rapor isteği reddedildi")
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                return schema.model_validate_json(text)
            except ValidationError:
                if attempt or not repair_budget[0]:
                    raise
                repair_budget[0] -= 1
                args["messages"].extend(
                    [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": "Şemaya uymayan alanları düzelt; yalnızca JSON döndür.",
                        },
                    ]
                )
        raise RuntimeError("Rapor doğrulanamadı")

    async def batch(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        if not self.capabilities.batches:
            raise ValueError("Sağlayıcı toplu rapor desteklemiyor")
        return await operation(self.client)
