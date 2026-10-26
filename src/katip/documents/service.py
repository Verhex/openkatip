from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from katip.async_utils import LoopLocal
from katip.config import get_settings
from katip.documents.process import parse_subprocess
from katip.documents.retrieval import Evidence, retrieve
from katip.store import crypto
from katip.store.db import session_scope, write_scope
from katip.store.models import EncryptionInstallation, Meeting, PreparationDocument

_write_lock = LoopLocal(asyncio.Lock)


async def parse_document(data: bytes, kind: str) -> list[dict]:
    settings = get_settings()
    if len(data) > settings.document_max_bytes:
        raise ValueError("Belge boyutu sınırı aşıldı")
    if settings.parser_url:
        from katip.documents.client import parse_remote

        return await parse_remote(data, kind, settings)
    return await parse_subprocess(data, kind, settings, docker=settings.bot_backend == "docker")


async def list_documents(meeting_id: str) -> list[PreparationDocument]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(PreparationDocument)
                .where(PreparationDocument.meeting_id == meeting_id)
                .order_by(PreparationDocument.created_at, PreparationDocument.id)
            )
        )


async def save_document(
    meeting_id: str, name: str, data: bytes, pages: list[dict], *, presentation: bool = False
) -> PreparationDocument:
    async with file_guard(meeting_id):
        return await _save_document(meeting_id, name, data, pages, presentation=presentation)


@asynccontextmanager
async def file_guard(meeting_id: str):
    guard = crypto.file_lock(meeting_id)
    task = asyncio.create_task(asyncio.to_thread(guard.__enter__))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        await asyncio.to_thread(guard.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.to_thread(guard.__exit__, None, None, None)


def presentation_path(meeting_id: str, document_id: str) -> Path:
    if not re.fullmatch(r"[\w-]{1,64}", meeting_id) or not re.fullmatch(
        r"[a-f0-9]{32}", document_id
    ):
        raise ValueError("Geçersiz belge kimliği")
    return Path(get_settings().data_dir) / "presentations" / meeting_id / f"{document_id}.pdf"


async def _save_document(meeting_id, name, data, pages, *, presentation):
    if presentation:
        from katip.bot.raster import check_bytes

        check_bytes(data)
        if not name.lower().endswith(".pdf") or not crypto.available():
            raise ValueError("Sunum için PDF ve yapılandırılmış veri şifreleme anahtarı gerekli.")
    async with _write_lock.get(), write_scope() as s:
        # Serializes quota checks across replicas on PostgreSQL too.
        meeting = await s.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
        if meeting is None:
            raise LookupError("Toplantı bulunamadı")
        docs = list(
            await s.scalars(
                select(PreparationDocument).where(PreparationDocument.meeting_id == meeting_id)
            )
        )
        await s.run_sync(lambda sync: crypto.assert_live(sync.connection(), meeting_id))
        await s.run_sync(lambda sync: crypto.require_write_key(sync.connection()))
        cipher = (
            await s.run_sync(
                lambda sync: crypto.cipher_for(sync.connection(), meeting_id, create=True)
            )
            if crypto.available()
            else None
        )
        # Eski özet yalnız uyumluluk aramasında kullanılır; yeni kayıt toplantıya bağlıdır.
        legacy = hashlib.sha256(data).hexdigest()
        installation = await s.scalar(select(EncryptionInstallation.installation_id))
        fingerprint = crypto.document_fingerprint(installation, meeting_id, legacy)
        digest = cipher.document_digest(data) if cipher else fingerprint
        if existing := next((d for d in docs if d.sha256 in {digest, fingerprint, legacy}), None):
            existing.sha256 = digest
            existing.digest_version = 2 if cipher else 0
            if presentation:
                await asyncio.to_thread(
                    cipher._write_file, presentation_path(meeting_id, existing.id), data
                )
                existing.presentation = True
                existing.created_at = datetime.now(UTC)
            return existing
        if len(docs) >= get_settings().document_max_per_meeting:
            raise ValueError("Toplantı belge sınırı aşıldı")
        row = PreparationDocument(
            id=uuid.uuid4().hex,
            meeting_id=meeting_id,
            name=name,
            sha256=digest,
            pages=pages,
            digest_version=2 if cipher else 0,
            presentation=presentation,
        )
        if presentation:
            await asyncio.to_thread(cipher._write_file, presentation_path(meeting_id, row.id), data)
        s.add(row)
        await s.flush()
        return row


async def delete_document(meeting_id: str, document_id: str) -> bool:
    from katip.pipeline.session import registry

    if (session := registry.get(meeting_id)) and session._presentation_document_id == document_id:
        await session.presentation_action("stop")
    async with file_guard(meeting_id), write_scope() as s:
        row = await s.scalar(
            select(PreparationDocument).where(
                PreparationDocument.id == document_id, PreparationDocument.meeting_id == meeting_id
            )
        )
        if row is None:
            return False
        if row.presentation:
            await asyncio.to_thread(presentation_path(meeting_id, row.id).unlink, missing_ok=True)
        await s.delete(row)
        return True


async def erase_presentations(meeting_id: str, names: list[str], *, associated: bool) -> None:
    from katip.store.repo import _scrub

    async with file_guard(meeting_id), write_scope() as s:
        rows = list(
            await s.scalars(
                select(PreparationDocument).where(
                    PreparationDocument.meeting_id == meeting_id,
                    PreparationDocument.presentation.is_(True),
                )
            )
        )
        affected = [row for row in rows if associated or _scrub(row.pages, names) != row.pages]
        for row in affected:
            await asyncio.to_thread(presentation_path(meeting_id, row.id).unlink, missing_ok=True)
            await s.delete(row)
    from katip.pipeline.session import registry

    if affected and (session := registry.get(meeting_id)):
        await session.presentation_action("stop")


async def presentation_document(meeting_id: str):
    docs = await list_documents(meeting_id)
    return next((d for d in reversed(docs) if d.presentation), None)


async def document_evidence(meeting_id: str, query: str = "") -> Evidence:
    return retrieve(await list_documents(meeting_id), query, get_settings().document_context_chars)


async def document_context(meeting_id: str, query: str = "") -> str:
    return (await document_evidence(meeting_id, query)).text
