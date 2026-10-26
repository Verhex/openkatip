"""İç ağda kimlik doğrulamalı, sınırlı ve tek işlemli belge parser servisi."""

from __future__ import annotations

import asyncio
import secrets
from typing import Literal

from fastapi import FastAPI, HTTPException, Request

from katip.config import Settings
from katip.documents.client import parser_key
from katip.documents.process import parse_subprocess


def create_app(settings: Settings | None = None) -> FastAPI:
    # API .env dosyası veya sağlayıcı ayarları yüklenmez.
    settings = settings if settings is not None else Settings(_env_file=None)
    key = parser_key(settings)  # Eksik/zayıf anahtarla servis başlamaz.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    busy = asyncio.Lock()

    @app.post("/parse")
    async def parse(
        request: Request, kind: Literal["txt", "pdf", "pdf-presentation", "docx", "pptx"]
    ):
        given = request.headers.get("authorization", "")
        if not secrets.compare_digest(given.encode(), ("Bearer " + key).encode()):
            raise HTTPException(401, "Parser kimliği geçersiz")
        if busy.locked():
            raise HTTPException(429, "Parser meşgul")
        async with busy:
            try:
                length = request.headers.get("content-length")
                if length is not None and int(length) > settings.document_max_bytes:
                    raise HTTPException(413, "Belge boyutu sınırı aşıldı")
                data = bytearray()
                async with asyncio.timeout(settings.document_parser_timeout_s):
                    async for chunk in request.stream():
                        if len(data) + len(chunk) > settings.document_max_bytes:
                            raise HTTPException(413, "Belge boyutu sınırı aşıldı")
                        data.extend(chunk)
                pages = await parse_subprocess(bytes(data), kind, settings)
            except TimeoutError as exc:
                raise HTTPException(504, "Belge işleme süresi aşıldı") from exc
            except ValueError as exc:
                raise HTTPException(
                    422, "Belge okunamadı veya güvenli işlem sınırı aşıldı"
                ) from exc
            return {"pages": pages}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8001, access_log=False)


if __name__ == "__main__":
    main()
