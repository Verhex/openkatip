"""Ayarlardan sağlayıcılar; toplantı ve iş yükü boyunca yapışkan seçim."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import anthropic
import httpx2

from katip import usage
from katip.config import Settings, get_settings
from katip.logging import get_logger
from katip.providers.interfaces import Capabilities, TTSProvider
from katip.providers.llm import ClaudeProvider
from katip.providers.resilience import CircuitBreaker, transient

log = get_logger(__name__)
_MIN_ATTEMPT_S = 0.05
Workload = Literal["live", "translation", "report"]


class RoutedLLM:
    def __init__(self, registry: ProviderRegistry, workload: Workload, factory=None):
        self.registry = registry
        self.workload = workload
        self.factory = factory
        self.messages = self
        self.local_index = 0

    def _key(self):
        return (usage.current_meeting(), self.workload)

    def _index(self):
        key = self._key()
        return self.registry.sticky.get(key, 0) if key[0] else self.local_index

    def _switch(self, index):
        self.local_index = max(self.local_index, index)
        if self._key()[0]:
            self.registry.sticky[self._key()] = max(self._index(), index)

    def deadline_s(self):
        s = self.registry.settings
        return s.report_deadline_s if self.workload == "report" else s.provider_deadline_s

    def attempt_s(self):
        s = self.registry.settings
        return (
            s.report_attempt_timeout_s
            if self.workload == "report"
            else s.provider_attempt_timeout_s
        )

    async def run(
        self,
        operation: Callable[[ClaudeProvider], Awaitable[Any]],
        *,
        deadline=None,
        attempt_timeout=None,
    ):
        end = time.monotonic() + (deadline if deadline is not None else self.deadline_s())
        chain = self.registry.chain(self.workload)
        last = None
        for index in range(self._index(), len(chain)):
            name = chain[index]
            breaker = self.registry.attempt_breaker(name, self.workload, index, chain)
            if breaker is False:
                continue
            remaining = end - time.monotonic()
            if remaining < _MIN_ATTEMPT_S:
                # Zamanlayıcı saat çözünürlüğü kadar erken tetiklenebilir (Windows ~15 ms);
                # birkaç milisaniyeyle sonraki sağlayıcıya istek başlatılmaz.
                if breaker:
                    breaker.release()
                raise TimeoutError("Sağlayıcı toplam süresi doldu") from last
            try:
                last_provider = index == len(chain) - 1
                limit = (
                    remaining
                    if last_provider
                    else min(remaining, attempt_timeout or self.attempt_s())
                )
                provider = self.registry.for_attempt(
                    name, self.factory, self.workload, last_provider, min(limit, self.deadline_s())
                )
                self._switch(index)
                async with asyncio.timeout_at(min(end, time.monotonic() + limit)):
                    result = await operation(provider)
            except asyncio.CancelledError:
                if breaker:
                    breaker.release()
                raise
            except Exception as exc:
                if not transient(exc):
                    self.registry.observed[name] = False
                    if breaker:
                        breaker.release()
                    raise
                if breaker:
                    breaker.failure()
                self.registry.observed[name] = False
                last = exc
                if index + 1 < len(chain):
                    self._switch(index + 1)
                    log.warning("LLM sağlayıcısı değişiyor", provider=name, workload=self.workload)
                if end - time.monotonic() < _MIN_ATTEMPT_S:
                    raise
            else:
                if breaker:
                    breaker.success()
                self.registry.observed[name] = True
                return result
        if last:
            raise last
        raise ConnectionError("Sağlayıcı devreleri açık")

    async def create(self, **params):
        return await self.run(lambda provider: provider.create(**params))

    async def structured(self, schema, **params):
        repair_budget = [1]
        return await self.run(
            lambda provider: provider.structured(schema, repair_budget=repair_budget, **params)
        )

    @asynccontextmanager
    async def stream(self, **params):
        stream = RoutedStream(self, params)
        try:
            yield stream
        finally:
            await stream.text_stream.aclose()


class RoutedStream:
    def __init__(self, route: RoutedLLM, params: dict):
        self.route = route
        self.params = params
        self.final = None
        self.text_stream = self._pieces()

    async def _pieces(self):
        route = self.route
        end = time.monotonic() + route.deadline_s()
        emitted = ""
        chain = route.registry.chain(route.workload)
        last = None
        for index in range(route._index(), len(chain)):
            breaker = route.registry.attempt_breaker(chain[index], route.workload, index, chain)
            if breaker is False:
                continue
            remaining = end - time.monotonic()
            if remaining < _MIN_ATTEMPT_S:
                # Zamanlayıcı saat çözünürlüğü kadar erken tetiklenebilir (Windows ~15 ms);
                # birkaç milisaniyeyle sonraki sağlayıcıya istek başlatılmaz.
                if breaker:
                    breaker.release()
                raise TimeoutError("Sağlayıcı toplam süresi doldu") from last
            try:
                last_provider = index == len(chain) - 1
                limit = remaining if last_provider else min(remaining, route.attempt_s())
                p = route.registry.for_attempt(
                    chain[index], route.factory, route.workload, last_provider, limit
                )
                route._switch(index)
                prefix = ""
                expected = emitted
                async with asyncio.timeout_at(min(end, time.monotonic() + limit)):
                    async with p.client.messages.stream(**p.params(self.params)) as stream:
                        async for piece in stream.text_stream:
                            if expected and len(prefix) < len(expected):
                                prefix += piece
                                if not expected.startswith(prefix) and not prefix.startswith(
                                    expected
                                ):
                                    raise ValueError(
                                        "Çeviri yedeğinin başlangıcı farklı; parça birleştirilemedi"
                                    )
                                if len(prefix) <= len(expected):
                                    continue
                                piece = prefix[len(expected) :]
                            emitted += piece
                            yield piece
                        if getattr(stream, "get_final_message", None) is not None:
                            self.final = p.annotate(
                                await stream.get_final_message(), self.params["model"]
                            )
            except (asyncio.CancelledError, GeneratorExit):
                if breaker:
                    breaker.release()
                raise
            except Exception as exc:
                if not transient(exc):
                    route.registry.observed[chain[index]] = False
                    if breaker:
                        breaker.release()
                    raise
                if breaker:
                    breaker.failure()
                route.registry.observed[chain[index]] = False
                last = exc
                if index + 1 < len(chain):
                    route._switch(index + 1)
                if end - time.monotonic() < _MIN_ATTEMPT_S:
                    raise
            else:
                if breaker:
                    breaker.success()
                route.registry.observed[chain[index]] = True
                return
        if last:
            raise last
        raise ConnectionError("Sağlayıcı devreleri açık")

    async def get_final_message(self):
        return self.final


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sticky: dict[tuple[str, str], int] = {}
        self.breakers: dict[tuple[str, str], CircuitBreaker] = {}
        self.clients: dict[str, ClaudeProvider] = {}
        self.observed: dict[str, bool] = {}

    def chain(self, workload: Workload) -> list[str]:
        return (
            self.settings.llm_report_chain if workload == "report" else self.settings.llm_live_chain
        )

    def breaker(self, name: str, workload: str) -> CircuitBreaker:
        return self.breakers.setdefault((name, workload), CircuitBreaker())

    def attempt_breaker(self, name, workload, index, chain):
        if len(chain) == 1:
            return None
        breaker = self.breaker(name, workload)
        # Son yedek, diğer toplantıların kesintilerinden dolayı engellenmez.
        if index == len(chain) - 1 or breaker.acquire():
            return breaker
        return False

    def for_attempt(self, name, factory, workload, last_provider, timeout):
        provider = self.llm_provider(name, factory)
        # SDK kopyası aynı taşıma havuzunu paylaşır; başka iş yükünün ayarı değişmez.
        client = provider.client
        if hasattr(client, "with_options"):
            client = client.with_options(
                max_retries=2 if last_provider else 0,
                timeout=httpx2.Timeout(timeout, connect=min(5, timeout)),
            )
        return ClaudeProvider(
            provider.name,
            provider.region,
            client,
            provider.capabilities,
            model_prefix=self.settings.bedrock_model_prefix,
            model_override=self.settings.bedrock_model_ids.get(workload),
        )

    def llm(self, workload: Workload = "live", *, anthropic_factory=None) -> RoutedLLM:
        return RoutedLLM(self, workload, anthropic_factory)

    def llm_provider(self, name: str, factory=None) -> ClaudeProvider:
        if name in self.clients:
            return self.clients[name]
        s = self.settings
        options = {}
        if name == "anthropic":
            client = (factory or anthropic.AsyncAnthropic)(
                api_key=s.anthropic_api_key or None, **options
            )
            region = "api"
            caps = Capabilities(
                prompt_cache_1h=True,
                batches=True,
                structured_output=True,
                output_config=True,
                thinking_config=True,
                thinking_signatures=True,
            )
        elif name == "bedrock":
            # Runtime istemcisi varsayılan olarak EU inference profile kullanır.
            client = anthropic.AsyncAnthropicBedrock(
                aws_region=s.bedrock_region, aws_profile=s.bedrock_aws_profile or None, **options
            )
            region = s.bedrock_region
            caps = Capabilities()
        elif name == "vertex":
            client = anthropic.AsyncAnthropicVertex(
                project_id=s.vertex_project, region=s.vertex_region, **options
            )
            region = s.vertex_region
            caps = Capabilities(prompt_cache_1h=True, structured_output=True)
        else:
            raise ValueError("Bilinmeyen LLM sağlayıcısı")
        provider = ClaudeProvider(name, region, client, caps, model_prefix=s.bedrock_model_prefix)
        self.clients[name] = provider
        return provider

    @property
    def stt_chain(self) -> list[str]:
        return self.settings.stt_chain or [self.settings.stt_provider]

    def stt(self, name: str | None = None):
        name = name or self.stt_chain[0]
        if name == "soniox":
            from katip.pipeline.stt.soniox import SonioxSTT

            return SonioxSTT(settings=self.settings)
        if name == "assemblyai":
            from katip.pipeline.stt.assemblyai import AssemblyAISTT

            return AssemblyAISTT(settings=self.settings)
        if name == "deepgram":
            from katip.pipeline.stt.deepgram import DeepgramSTT

            return DeepgramSTT(settings=self.settings)
        raise ValueError("Bilinmeyen STT sağlayıcısı")

    def tts(self) -> TTSProvider:
        from katip.voice.tts import SonioxTTS

        return SonioxTTS(settings=self.settings)

    def configured(self, name: str) -> bool:
        s = self.settings
        return {
            "anthropic": bool(s.anthropic_api_key),
            "bedrock": bool(s.bedrock_region),
            "vertex": bool(s.vertex_project),
            "soniox": bool(s.soniox_api_key),
            "assemblyai": bool(s.assemblyai_api_key),
            "deepgram": bool(s.deepgram_api_key),
        }.get(name, False)

    def enabled(self, workload: Workload = "live") -> bool:
        return any(self.configured(n) for n in self.chain(workload))

    def forget(self, meeting_id: str) -> None:
        for key in list(self.sticky):
            if key[0] == meeting_id:
                del self.sticky[key]

    def health(self) -> dict:
        chains = {
            "live": self.chain("live"),
            "translation": self.chain("translation"),
            "report": self.chain("report"),
            "stt": self.stt_chain,
            "tts": ["soniox"],
        }
        names = dict.fromkeys(n for chain in chains.values() for n in chain)
        return {
            "chains": chains,
            "providers": {
                n: {
                    "configured": self.configured(n),
                    "ok": self.observed.get(n) if self.configured(n) else False,
                    "detail": (
                        "Ücretli çağrı yapılmadı; hesap erişimi doğrulanmadı"
                        if self.configured(n)
                        else "Yapılandırılmadı"
                    ),
                }
                for n in names
            },
            "active": {
                f"{meeting}:{workload}": (
                    self.stt_chain if workload == "stt" else self.chain(workload)
                )[index]
                for (meeting, workload), index in self.sticky.items()
            },
        }


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    settings = get_settings()
    if _registry is None or _registry.settings is not settings:
        _registry = ProviderRegistry(settings)
    return _registry
