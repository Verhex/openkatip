"""Kullanım ve maliyet takibi: her Claude çağrısı, STT akış süresi ve TTS süresi.

Toplantı başına toplanır ve `meeting_usage` tablosuna yazılır; `GET /api/usage` ile okunur.
Fiyatlar resmî sayfalardan (8 Ekim 2026). Değişirse yalnız buradaki tablo güncellenir:
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- Soniox: https://soniox.com/pricing (STT sessizlik dahil akış süresiyle faturalanır)

Hangi toplantıya ait olduğu bağlam değişkeniyle taşınır: çağrıyı yapan kod
`with usage.meeting(meeting_id):` bloğu içinde Claude'u çağırır; alt fonksiyonların imzası
değişmez.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass

from katip.logging import get_logger

log = get_logger(__name__)

_current: ContextVar[str | None] = ContextVar("katip_usage_meeting", default=None)
_source: ContextVar[tuple | None] = ContextVar("katip_usage_source", default=None)


def current_meeting() -> str | None:
    return _current.get()


def annotate(tokens, provider: str, model: str, region: str) -> None:
    _source.set((tokens, provider, model, region))


@dataclass(frozen=True)
class LlmPrice:
    input: float  # USD / 1M token
    output: float
    cache_write: float  # 5 dk önbellek yazma
    cache_read: float
    long: LlmPrice | None = None
    long_threshold: int = 100_000


LLM_PRICES: dict[str, LlmPrice] = {
    "claude-sonnet-5-5": LlmPrice(2.0, 10.0, 2.5, 0.2),
    "claude-haiku-5-5": LlmPrice(0.1, 0.5, 0.125, 0.01, long=LlmPrice(0.5, 2.5, 0.625, 0.05)),
    "claude-haiku-4-5": LlmPrice(1.0, 5.0, 1.25, 0.1),
    "claude-opus-5-5": LlmPrice(4.0, 20.0, 5.0, 0.2),
}
_DEFAULT_PRICE = LLM_PRICES["claude-sonnet-5-5"]
_warned_models: set[str] = set()

STT_USD_PER_HOUR = 0.12  # stt-rt-v5, akış süresi (sessizlik dahil)
TTS_USD_PER_HOUR = 0.70  # tts-rt-v2, üretilen konuşma süresi
BATCH_DISCOUNT = 0.5


@contextlib.contextmanager
def meeting(meeting_id: str | None):
    token = _current.set(meeting_id)
    try:
        yield
    finally:
        _current.reset(token)


def llm_cost(
    model: str, usage, *, batch: bool = False, provider="anthropic", region="api"
) -> float:
    from katip.config import get_settings

    model = model.removeprefix("eu.").removeprefix("anthropic.")
    override = get_settings().provider_llm_prices.get(f"{provider}/{model}/{region}")
    price = LlmPrice(**override) if override else LLM_PRICES.get(model)
    if price is None:
        if model not in _warned_models:
            _warned_models.add(model)
            log.warning("bilinmeyen model, varsayılan fiyat kullanılıyor", model=model)
        price = _DEFAULT_PRICE
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    if price.long is not None and input_tokens + read + written > price.long_threshold:
        price = price.long
    # 1 saatlik önbellek yazma 2x, 5 dakikalık 1,25x (ayrım usage.cache_creation'da).
    breakdown = getattr(usage, "cache_creation", None)
    written_1h = (getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0) if breakdown else 0
    cost = (
        input_tokens * price.input
        + (getattr(usage, "output_tokens", 0) or 0) * price.output
        + (written - written_1h) * price.cache_write
        + written_1h * price.input * 2
        + read * price.cache_read
    ) / 1_000_000
    return cost * (BATCH_DISCOUNT if batch else 1.0)


async def record_llm(kind: str, model: str, usage, *, batch: bool = False) -> None:
    """Bir Claude çağrısının kullanımını loglar ve toplantının toplamına ekler."""
    if usage is None:
        return
    source = _source.get()
    provider, region = "anthropic", "api"
    if source and source[0] is usage:
        _, provider, model, region = source
    cost = llm_cost(model, usage, batch=batch, provider=provider, region=region)
    meeting_id = _current.get()
    fields = {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    log.info(
        "LLM kullanımı",
        kind=kind,
        meeting_id=meeting_id,
        provider=provider,
        model=model,
        region=region,
        usd=round(cost, 5),
        **fields,
    )
    if meeting_id:
        await _add(
            meeting_id,
            provider_key=f"{provider}/{model}/{region}",
            llm_calls=1,
            llm_usd=cost,
            **fields,
        )


async def record_stt(
    meeting_id: str, seconds: float, *, provider="soniox", model="stt-rt-v5", region=None
) -> None:
    from katip.config import get_settings

    if seconds <= 0:
        return
    s = get_settings()
    region = region or s.soniox_region
    key = f"{provider}/{model}/{region}"
    # Yeni sağlayıcının ücreti hesap sözleşmesinden girilene kadar tahmin bilinmiyor.
    price = s.provider_stt_prices.get(key, STT_USD_PER_HOUR if provider == "soniox" else 0)
    cost = seconds / 3600 * price
    log.info(
        "stt kullanımı",
        meeting_id=meeting_id,
        provider=provider,
        model=model,
        region=region,
        seconds=round(seconds, 1),
        usd=round(cost, 4),
    )
    await _add(meeting_id, provider_key=key, stt_seconds=seconds, stt_usd=cost)


async def record_tts(meeting_id: str, seconds: float) -> None:
    if seconds <= 0:
        return
    from katip.config import get_settings

    s = get_settings()
    await _add(
        meeting_id,
        provider_key=f"soniox/{s.tts_model}/{s.soniox_region}",
        tts_seconds=seconds,
        tts_usd=seconds / 3600 * TTS_USD_PER_HOUR,
    )


async def _add(meeting_id: str, **deltas) -> None:
    from katip.store import repo

    try:
        await repo.add_usage(meeting_id, **deltas)
    except Exception as exc:  # kullanım kaydı canlı akışı asla bozmamalı
        log.debug("kullanım kaydedilemedi", error=str(exc))
