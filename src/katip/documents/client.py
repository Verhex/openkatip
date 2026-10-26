"""API → izole parser; hata halinde yerel parser'a geri dönülmez."""

from __future__ import annotations

from pathlib import Path

import httpx

from katip.config import Settings
from katip.documents.parser import MAX_PAGES, MAX_TEXT
from katip.workers.manager import api_url


def parser_key(settings: Settings) -> str:
    key = (
        Path(settings.parser_key_file).read_text(encoding="utf-8").strip()
        if settings.parser_key_file
        else settings.parser_key
    )
    if len(key) < 32 or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("Parser anahtarı en az 32 ASCII karakter olmalı; boşluk içeremez")
    return key


async def parse_remote(data: bytes, kind: str, settings: Settings) -> list[dict]:
    base = api_url(settings.parser_url, "parser")
    key = parser_key(settings)
    try:
        async with httpx.AsyncClient(
            base_url=base,
            timeout=settings.document_parser_timeout_s + 5,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/parse",
                params={"kind": kind},
                content=data,
                headers={"Authorization": "Bearer " + key},
            )
            response.raise_for_status()
            pages = response.json().get("pages")
    except httpx.HTTPError as exc:
        raise ValueError("İzole belge işleme servisine erişilemedi veya belge reddedildi") from exc
    if (
        not isinstance(pages, list)
        or not 1 <= len(pages) <= MAX_PAGES
        or any(
            not isinstance(p, dict)
            or set(p) != {"location", "text"}
            or not isinstance(p["location"], str)
            or len(p["location"]) > 100
            or not isinstance(p["text"], str)
            for p in pages
        )
        or sum(len(p["text"]) for p in pages) > MAX_TEXT
    ):
        raise ValueError("Belge işleme sonucu geçersiz")
    return pages
