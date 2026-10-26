"""E-posta tabanlı katılımcı kimliği; görünen ad yetki sağlamaz."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_GUEST_SUFFIX = re.compile(r"\s*\((guest|external|misafir|konuk|dış)\)\s*$", re.I)


def participant_name(value: str) -> str:
    return _GUEST_SUFFIX.sub("", value.strip()).casefold()


def normalize_email(value: str) -> str:
    value = value.strip()
    if not _EMAIL.fullmatch(value):
        raise ValueError("Geçerli bir e-posta adresi girin")
    local, domain = value.rsplit("@", 1)
    # Alan adını Unicode harf dönüşümünden önce denetle; yerel kısım eski kimliklerle uyumludur.
    if not domain.isascii():
        raise ValueError("E-posta alan adı yalnız ASCII karakterleri içerebilir")
    value = f"{local.casefold()}@{domain.lower()}"
    if len(value) > 320 or not _EMAIL.fullmatch(value):
        raise ValueError("Geçerli bir e-posta adresi girin")
    return value


def company_email(value: str, domains: list[str]) -> bool:
    try:
        domain = normalize_email(value).rsplit("@", 1)[1]
    except ValueError:
        return False
    return domain in {d.strip().lower() for d in domains if d.isascii()}


class ParticipantIdentity(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str
    platform_id: str | None = Field(None, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Katılımcı adı boş olamaz veya kontrol karakteri içeremez")
        return value
