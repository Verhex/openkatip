"""Sağlayıcı sözleşmeleri; gelecekteki OpenAI adaptörü de bu sınırı kullanabilir."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from katip.pipeline.stt.base import Utterance


@dataclass(frozen=True)
class Capabilities:
    prompt_cache_1h: bool = False
    batches: bool = False
    structured_output: bool = False
    translation: bool = False
    diarization: bool = False
    output_config: bool = False
    thinking_config: bool = False
    thinking_signatures: bool = False


class LLMProvider(Protocol):
    name: str
    region: str
    capabilities: Capabilities

    async def create(self, **params: Any) -> Any: ...

    def stream(self, **params: Any) -> AsyncIterator[str]: ...

    async def structured(self, schema: type[BaseModel], **params: Any) -> BaseModel: ...

    async def batch(self, operation: Callable[[Any], Awaitable[Any]]) -> Any: ...


class RealtimeSTT(Protocol):
    name: str
    region: str
    capabilities: Capabilities
    translates: bool

    def stream(
        self, audio: AsyncIterator[bytes], *, language: str, target_language: str | None = None
    ) -> AsyncIterator[Utterance]: ...


class TTSProvider(Protocol):
    sample_rate: int

    def synthesize(self, text: str, *, language: str) -> AsyncIterator[bytes]: ...
