"""Toplantı transkriptinden yapılandırılmış rapor (Claude, structured output)."""

from __future__ import annotations

import asyncio
import contextlib
import time

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from katip import usage
from katip.agent.llm import thinking_off
from katip.agent.security import DATA_POLICY
from katip.config import get_settings
from katip.logging import get_logger
from katip.providers import get_registry
from katip.providers.llm import server_fallback
from katip.providers.resilience import ProviderOutage

log = get_logger(__name__)


class Decision(BaseModel):
    text: str = Field(description="Alınan karar, tek cümle")
    owner: str | None = Field(None, description="Kararı veren/sahiplenen kişi, belliyse")


class ActionItem(BaseModel):
    owner: str | None = Field(
        None, description="Sorumlu kişi (şemadaki tam adıyla), belli değilse boş"
    )
    task: str = Field(description="Yapılacak iş, fiille başlayan kısa cümle")
    due: str | None = Field(
        None, description="Termin, konuşmada geçtiği şekliyle ('Cuma', '15 Ekim')"
    )
    priority: str | None = Field(
        None, description="yüksek / orta / düşük, konuşmadan çıkarılabiliyorsa"
    )


class ParticipantNote(BaseModel):
    name: str
    highlights: list[str] = Field(description="Bu kişinin öne çıkan katkıları, 1-3 madde")


class MeetingReport(BaseModel):
    title: str = Field(
        description="Toplantının konusu: kısa, somut başlık (ör. 'Q4 pazarlama bütçesi')"
    )
    summary: str = Field(description="3-6 cümlelik yönetici özeti")
    key_points: list[str] = Field(description="Konuşulan ana başlıklar")
    decisions: list[Decision]
    action_items: list[ActionItem]
    open_questions: list[str] = Field(description="Cevapsız kalan ya da takip gerektiren sorular")
    participants: list[ParticipantNote]


_SYSTEM = (
    DATA_POLICY
    + "\n\n"
    + """Sen bir şirketin toplantı sekreterisin.
Sana bir toplantının konuşmacı etiketli
transkripti ve (varsa) şirket şeması verilecek. Görevin, toplantıya katılmamış bir yöneticinin
okuyup her şeyi anlayacağı, doğru ve tarafsız bir rapor çıkarmak.

İlkeler:
- Yalnızca transkriptte geçenleri yaz; tahmin, yorum ya da uydurma ekleme.
- Karar ile öneriyi ayır: açıkça üzerinde anlaşılmayan bir şey karar değildir.
- Aksiyon sahiplerini şemadaki tam adlarıyla yaz; transkriptte "Konuşmacı 2" gibi etiketlenmiş
  ve kim olduğu anlaşılamayan kişiler için sahibi boş bırak.
- "Anonim katılımcı" olarak geçen kişinin kimliğini tahmin etmeye çalışma.
- <katip_notlari> içindeki kararları, aksiyonları ve notları mutlaka rapora al: bunları
  katılımcılar açıkça kaydettirdi. Ekran görüntüsü açıklamalarını ilgili bölüme bağla.
- Transkriptteki "Katip" konuşmacısı toplantı asistanıdır (yapay zekâ). Onu katılımcı
  sayma, katılımcı notlarına ekleme; cevaplarını yalnızca bağlam olarak kullan.
- Her alanı doldur; içerik yoksa listeyi boş bırak, uydurma madde ekleme.
- Raporu {lang} dilinde yaz."""
)


def format_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        minute, second = divmod(int(seg["start_s"]), 60)
        speaker = seg.get("speaker_name") or "Bilinmeyen"
        if seg.get("speaker_email"):
            speaker += f" <{seg['speaker_email']}>"
        lines.append(f"[{minute:02d}:{second:02d}] {speaker}: {seg['text']}")
    return "\n".join(lines)


_BATCH_POLL_S = 30


async def _summarize_batch(client, params: dict, on_batch, resume_batch: str | None, provider=None):
    from anthropic.lib._parse._transform import transform_schema

    params = {
        **params,
        "output_config": {
            "format": {"type": "json_schema", "schema": transform_schema(MeetingReport)}
        },
    }
    batch_id = resume_batch
    if not batch_id:
        created = await client.messages.batches.create(
            requests=[{"custom_id": "rapor", "params": params}]
        )
        batch_id = created.id
        log.info("rapor batch'e gönderildi", batch_id=batch_id)
        if on_batch is not None:
            await on_batch(batch_id)
    deadline = time.monotonic() + get_settings().report_batch_max_wait_s
    while (await client.messages.batches.retrieve(batch_id)).processing_status != "ended":
        if time.monotonic() > deadline:
            with contextlib.suppress(Exception):
                await client.messages.batches.cancel(batch_id)
            raise TimeoutError(f"rapor batch'i zamanında bitmedi: {batch_id}")
        await asyncio.sleep(_BATCH_POLL_S)
    async for entry in await client.messages.batches.results(batch_id):
        if entry.result.type != "succeeded":
            if entry.result.type == "errored":
                error_type = entry.result.error.error.type
                status = {"rate_limit_error": 429, "overloaded_error": 529, "api_error": 500}.get(
                    error_type
                )
                if status:
                    raise ProviderOutage(status)
            raise RuntimeError(f"rapor batch'i başarısız: {entry.result.type}")
        message = entry.result.message
        if provider is not None:
            provider.annotate(message, params["model"])
        await usage.record_llm("rapor", params["model"], message.usage, batch=True)
        text = "".join(b.text for b in message.content if b.type == "text")
        return MeetingReport.model_validate_json(text)
    raise RuntimeError(f"rapor batch'inde sonuç yok: {batch_id}")


def _fallback_args(model: str) -> dict:
    """Sunucu tarafı ret yedeği yalnızca bunu destekleyen modellerde açılır."""
    return server_fallback(model)


async def summarize_meeting(
    segments: list[dict],
    *,
    org_context: str = "",
    notes: str = "",
    lang: str = "Türkçe",
    client: AsyncAnthropic | None = None,
    batch: bool = False,
    on_batch=None,
    resume_batch: str | None = None,
    thinking: bool | None = None,
) -> MeetingReport:
    """Toplantı raporu. batch=True: Message Batches API (%50 indirim; sonuç genelde birkaç
    dakikada, en geç 24 saatte). on_batch(batch_id) sunucu yeniden başlarsa devam edebilmek
    için kimliği saklar; resume_batch ile var olan batch'in sonucu beklenir."""
    settings = get_settings()

    parts = []
    if org_context:
        parts.append(f"<sirket_semasi>\n{org_context}\n</sirket_semasi>")
    if notes:
        # Katılımcıların Katip'e açıkça aldırdığı notlar: raporda mutlaka yer almalı.
        parts.append(f"<katip_notlari>\n{notes}\n</katip_notlari>")
    parts.append(f"<transkript>\n{format_transcript(segments)}\n</transkript>")

    params = {
        "model": settings.llm_model,
        "max_tokens": 16000,
        "system": _SYSTEM.format(lang=lang),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
        **(
            {}
            if (settings.report_thinking if thinking is None else thinking)
            else thinking_off(settings.llm_model)
        ),
    }
    if client is None:
        repair_budget = [1]

        async def generate(provider):
            # Bulut yedeklerinde Message Batches yoktur; normal çağrıya geçilir.
            # Geçişte önbellek soğuk başlar; toplantı içinde birincile geri dönülmez.
            if batch and provider.capabilities.batches:
                try:
                    return await _summarize_batch(
                        provider.client, params, on_batch, resume_batch, provider
                    )
                except TimeoutError:
                    log.warning("rapor batch'i gecikti, anında üretiliyor")
            async with asyncio.timeout(settings.report_deadline_s):
                return await provider.structured(
                    MeetingReport, repair_budget=repair_budget, **params
                )

        deadline = (
            settings.report_batch_max_wait_s + settings.report_deadline_s
            if batch
            else settings.report_deadline_s
        )
        return (
            await get_registry()
            .llm("report")
            .run(generate, deadline=deadline, attempt_timeout=deadline if batch else None)
        )
    if batch:
        try:
            return await _summarize_batch(client, params, on_batch, resume_batch)
        except TimeoutError as exc:
            # Batch geç kaldı: rapor gecikmesin, anında üret (batch iptal edildi).
            log.warning("rapor batch'i gecikti, anında üretiliyor", error=str(exc))
    response = await client.beta.messages.parse(
        **params, output_format=MeetingReport, **_fallback_args(settings.llm_model)
    )
    await usage.record_llm("rapor", settings.llm_model, response.usage)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"rapor üretilemedi (stop_reason={response.stop_reason})")
    return response.parsed_output
