"""Panelden değiştirilebilen ayarlar.

Değerler `app_settings` tablosunda tutulur ve ortam değişkenlerinin üstüne
yazılır: açılışta ve her değişiklikte çalışan ayar nesnesine uygulanır. Yeni toplantılar yeni
değerlerle başlar; bazıları (cevap modu, çeviri dili) süren toplantıyı etkilemez.

Gizli değerler (API anahtarları, şifreleme anahtarları, yönetici token'ı) burada DEĞİLDİR;
onlar yalnız korunan kurulum yapılandırmasındadır ve panelden görülemez.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from katip.config import get_settings, organizer_domains, organizer_emails
from katip.integrations.calendar import guarded
from katip.logging import get_logger
from katip.store import repo
from katip.store.models import AppSetting, AuditLog

log = get_logger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class EditableSettings(BaseModel):
    """Panel formundaki alanlar (hepsi isteğe bağlı: yalnız gönderilenler değişir)."""

    company_name: str | None = Field(None, max_length=100, description="Şirket adı")
    bot_display_name: str | None = Field(
        None, min_length=2, max_length=60, description="Toplantıda görünen bot adı"
    )
    default_mode: Literal["tam", "kayit"] | None = None
    reply_mode: Literal["chat", "voice"] | None = None
    source_lang: str | None = Field(None, pattern=r"^[a-z]{2}$")
    target_lang: str | None = Field(None, pattern=r"^([a-z]{2})?$", description="Boş = çeviri yok")
    tts_voice: str | None = Field(None, min_length=2, max_length=40)
    privacy_notice_on_join: Literal["chat", "voice", "off"] | None = None
    privacy_contact: str | None = Field(None, max_length=200)
    privacy_server_location: str | None = Field(None, min_length=1, max_length=300)
    retention_days: int | None = Field(None, ge=0, le=3650)
    post_report_to_chat: bool | None = None
    report_batch: bool | None = None
    share_link_days: float | None = Field(None, ge=1, le=30)
    max_meeting_minutes: int | None = Field(None, ge=10, le=240)
    record_max_minutes: int | None = Field(None, ge=30, le=720)
    daily_meeting_minutes: int | None = Field(None, ge=30, le=1440)
    max_agent_calls_per_meeting: int | None = Field(None, ge=5, le=500)
    calendar_join_policy: Literal["kapalı", "onay", "güvenilir", "hepsi"] | None = None
    calendar_organizer_domains: list[str] | None = Field(None, max_length=100)
    calendar_organizer_emails: list[str] | None = Field(None, max_length=200)
    calendar_daily_autojoins: int | None = Field(None, ge=0, le=1000)
    calendar_max_pending: int | None = Field(None, ge=1, le=1000)

    @field_validator("calendar_organizer_domains", "calendar_organizer_emails")
    @classmethod
    def _organizers(cls, value, info):
        if value is None:
            return None
        validator = organizer_domains if info.field_name.endswith("domains") else organizer_emails
        return validator(value)

    @field_validator(
        "company_name",
        "bot_display_name",
        "privacy_contact",
        "privacy_server_location",
        "tts_voice",
    )
    @classmethod
    def _clean(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if info.field_name == "privacy_server_location" and not value:
            raise ValueError("Sunucu konumu cümlesi boş olamaz")
        if _CONTROL.search(value):
            raise ValueError("kontrol karakteri içeremez")
        return value


EDITABLE_KEYS = tuple(EditableSettings.model_fields)


def current() -> dict:
    s = get_settings()
    return {key: getattr(s, key) for key in EDITABLE_KEYS}


def _apply(values: dict) -> None:
    s = get_settings()
    for key, value in values.items():
        if key in EDITABLE_KEYS:
            setattr(s, key, value)


async def load() -> None:
    """Açılışta: panelde kaydedilmiş ayarları çalışan ayarlara uygula."""
    try:
        stored = await repo.get_app_settings()
    except Exception as exc:
        log.warning("panel ayarları yüklenemedi", error=str(exc))
        return
    values = {k: v for k, v in stored.items() if k in EDITABLE_KEYS}
    try:
        values = EditableSettings.model_validate(values).model_dump(exclude_unset=True)
    except Exception as exc:
        log.warning("panel ayarları geçersiz, yok sayıldı", error=str(exc))
        return
    _apply(values)
    if values:
        log.info("panel ayarları uygulandı", keys=sorted(values))


async def update(changes: EditableSettings, *, actor: str = "admin") -> dict:
    values = changes.model_dump(exclude_unset=True)
    now = current()
    # Takvim ayarı ortamla aynı olsa da saklanır; sonraki açılışta panel değeri kazanır.
    values = {
        k: v
        for k, v in values.items()
        if (v is not None or k == "company_name") and (k.startswith("calendar_") or v != now.get(k))
    }
    if not values:
        return current()
    async with guarded() as session:
        await _store(session, values, actor)
    return current()


async def _store(session, values: dict, actor: str) -> None:
    for key, value in values.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value={"v": value}))
        else:
            row.value = {"v": value}
            row.updated_at = datetime.now(UTC)
    session.add(AuditLog(actor=actor, action="settings_changed", detail={"keys": sorted(values)}))
    await session.flush()
    _apply(values)


async def trust_calendar_organizer(email: str, *, actor: str = "admin") -> dict:
    """Tek adresi veritabanı kilidi altında ekler; eşzamanlı eklemeler kaybolmaz."""
    email = organizer_emails([email])[0]
    async with guarded() as session:
        row = await session.get(AppSetting, "calendar_organizer_emails")
        existing = row.value["v"] if row else get_settings().calendar_organizer_emails
        emails = organizer_emails([*existing, email])
        # Model doğrulaması liste sınırını da uygular.
        values = EditableSettings(calendar_organizer_emails=emails).model_dump(exclude_unset=True)
        await _store(session, values, actor)
    return current()
