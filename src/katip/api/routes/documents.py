from __future__ import annotations

import asyncio
import uuid
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from katip.api.security import (
    PanelAccess,
    require_meeting_manage,
    require_meeting_read,
    require_operator,
)
from katip.config import get_settings
from katip.documents import service
from katip.store import repo

router = APIRouter(prefix="/api/preparations", tags=["preparation"])
_parser_slots = asyncio.Semaphore(2)


class PreparationRequest(BaseModel):
    title: str = Field("", max_length=200)


@router.post("")
async def prepare(
    req: PreparationRequest, access: Annotated[PanelAccess, Depends(require_operator)]
) -> dict:
    meeting_id = uuid.uuid4().hex[:12]
    await repo.upsert_meeting(
        meeting_id,
        state="prepared",
        title=req.title.strip() or None,
        owner_account_id=access.account_id,
    )
    return {"session_id": meeting_id}


@router.get("/{meeting_id}/documents", dependencies=[Depends(require_meeting_read)])
async def documents(meeting_id: str) -> list[dict]:
    if await repo.get_meeting(meeting_id) is None:
        raise HTTPException(404, "Toplantı bulunamadı")
    return [metadata(d) for d in await service.list_documents(meeting_id)]


@router.delete("/{meeting_id}", dependencies=[Depends(require_meeting_manage)])
async def discard(meeting_id: str) -> dict:
    if not await repo.delete_meeting(meeting_id, state="prepared"):
        raise HTTPException(409, "Hazırlık bulunamadı veya toplantı başlamış")
    return {"deleted": True}


def metadata(doc) -> dict:
    return {
        "id": doc.id,
        "name": doc.name,
        "sha256": doc.sha256,
        "sections": len(doc.pages),
        "presentation": doc.presentation,
    }


@router.post("/{meeting_id}/documents", dependencies=[Depends(require_meeting_manage)])
async def upload(
    meeting_id: str, request: Request, filename: str, presentation: bool = False
) -> dict:
    # The administrator explicitly shares these contents with all meeting participants.
    if await repo.get_meeting(meeting_id) is None:
        raise HTTPException(404, "Toplantı bulunamadı")
    name = PurePosixPath(filename.replace("\\", "/")).name.strip()
    if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
        raise HTTPException(422, "Geçersiz dosya adı")
    kind = name.rsplit(".", 1)[-1].lower()
    if presentation and kind != "pdf":
        raise HTTPException(415, "Sunum olarak yalnız PDF yükleyin.")
    if presentation:
        from katip.store import crypto

        if not crypto.available():
            raise HTTPException(409, "Sunum için veri şifreleme anahtarı yapılandırılmalı.")
    if kind not in {"pdf", "pptx", "docx", "txt"}:
        raise HTTPException(415, "PDF, PPTX, DOCX veya UTF-8 TXT yükleyin")
    data = bytearray()
    async for chunk in request.stream():
        limit = (
            min(get_settings().document_max_bytes, 8 * 1024 * 1024)
            if presentation
            else get_settings().document_max_bytes
        )
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "Dosya boyutu sınırı aşıldı")
        data.extend(chunk)
    if not data:
        raise HTTPException(422, "Dosya boş")
    try:
        async with _parser_slots:
            pages = await service.parse_document(
                bytes(data), "pdf-presentation" if presentation else kind
            )
        doc = await service.save_document(
            meeting_id, name, bytes(data), pages, presentation=presentation
        )
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(422, "Belge okunamadı veya işlem sınırı aşıldı") from exc
    except LookupError as exc:
        raise HTTPException(404, "Toplantı bulunamadı") from exc
    await repo.audit(
        "admin", "preparation_document_added", meeting_id=meeting_id, document_id=doc.id
    )
    return metadata(doc)


@router.delete(
    "/{meeting_id}/documents/{document_id}", dependencies=[Depends(require_meeting_manage)]
)
async def remove(meeting_id: str, document_id: str) -> dict:
    if not await service.delete_document(meeting_id, document_id):
        raise HTTPException(404, "Belge bulunamadı")
    await repo.audit(
        "admin", "preparation_document_removed", meeting_id=meeting_id, document_id=document_id
    )
    return {"deleted": True}
