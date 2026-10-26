"""Düz içerik göçü ve DEK zarflarının toplu anahtar rotasyonu."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select, update

from katip.config import get_settings
from katip.logging import get_logger
from katip.store import crypto
from katip.store.db import session_scope, write_scope
from katip.store.models import Meeting, MeetingAsset, MeetingDataKey

log = get_logger(__name__)
state = "bekliyor"


async def rotate_keys(batch_size: int = 100) -> int:
    """İçeriğe dokunmadan eski KEK zarflarını yeni KEK ile değiştirir."""
    if batch_size < 1:
        raise ValueError("Rotasyon grup boyutu pozitif olmalı")
    current, _ = crypto.keys()
    if current is None:
        raise crypto.DataKeyError("Rotasyon için yeni veri anahtarı gerekli")
    changed = 0
    errors = 0
    last = ""
    while True:
        async with write_scope() as session:
            rows = list(
                await session.scalars(
                    select(MeetingDataKey)
                    .where(MeetingDataKey.meeting_id > last)
                    .order_by(MeetingDataKey.meeting_id)
                    .limit(batch_size)
                    .with_for_update()
                )
            )
            if not rows:
                if errors:
                    raise crypto.DataKeyError(
                        f"Anahtar rotasyonunda {errors} zarf işlenemedi; önceki anahtarı koruyun"
                    )
                return changed
            for row in rows:
                if row.key_id == crypto.key_id(current):
                    continue
                try:
                    async with session.begin_nested():
                        cipher = await session.run_sync(
                            lambda s, mid=row.meeting_id: crypto.cipher_for(s.connection(), mid)
                        )
                        aad = json.dumps(
                            ["katip-dek", 1, cipher.installation_id, row.meeting_id],
                            separators=(",", ":"),
                        ).encode()
                        wrapped = crypto.seal(current, cipher.key, aad)
                        if crypto.open_blob(current, wrapped, aad) != cipher.key:
                            raise crypto.DataKeyError("Yeni anahtar zarfı doğrulanamadı")
                        result = await session.execute(
                            update(MeetingDataKey)
                            .where(
                                MeetingDataKey.meeting_id == row.meeting_id,
                                MeetingDataKey.wrapped_key == row.wrapped_key,
                            )
                            .values(wrapped_key=wrapped, key_id=crypto.key_id(current))
                        )
                        changed += result.rowcount
                except Exception as exc:
                    errors += 1
                    _item_error("rotation", exc)
            last = rows[-1].meeting_id
        await asyncio.sleep(0)


async def _migrate_row(model, identity) -> int:
    primary = model.meeting_id if model is crypto.WorkerJob else model.id
    async with write_scope() as session:
        values = (
            (await session.execute(select(model.__table__).where(primary == identity)))
            .mappings()
            .first()
        )
        if values is None:
            return 0
        row = model(**dict(values))
        mid = crypto._meeting_id(row)
        if (
            await session.scalar(select(Meeting.id).where(Meeting.id == mid).with_for_update())
            is None
        ):
            return 0
        # Toplantı → içerik kilit sırası yazıcılarla aynıdır.
        values = (
            (
                await session.execute(
                    select(model.__table__).where(primary == identity).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if values is None:
            return 0
        row = model(**dict(values))
        await session.run_sync(lambda s: crypto.assert_live(s.connection(), mid))
        plain = values["encrypted_content"] is None
        if plain and await session.run_sync(lambda s: crypto.strict_reads(s.connection())):
            raise crypto.DataKeyError("Göç tamamlandıktan sonra düz satır reddedildi")
        legacy_lookup = any(
            isinstance(values[f], str) and values[f].startswith("km:") for f in crypto.FIELDS[model]
        )
        legacy_digest = model is crypto.PreparationDocument and values["digest_version"] != 2
        identity_model = model in {
            crypto.MeetingSegment,
            crypto.MeetingParticipant,
            crypto.MeetingIdentity,
        }
        if not (plain or legacy_lookup or legacy_digest or identity_model):
            return 0
        cipher = await session.run_sync(
            lambda s: crypto.cipher_for(s.connection(), mid, create=True)
        )
        content = (
            {f: values[f] for f in crypto.FIELDS[model]}
            if plain
            else crypto.decrypt_values(row, values["encrypted_content"], cipher)
        )
        payload = (
            crypto._encrypt_values(row, content, cipher) if plain else values["encrypted_content"]
        )
        changes = {f: crypto._placeholder(row, f, v, cipher) for f, v in content.items()}
        if not plain and not legacy_digest and all(values[f] == v for f, v in changes.items()):
            return 0
        changes["encrypted_content"] = payload
        if legacy_digest:
            digest = (
                cipher.keyed_document_fingerprint(values["sha256"])
                if values["digest_version"] == 0
                else cipher.digest_from_sha256(values["sha256"])
            )
            changes.update(sha256=digest, digest_version=2)
        await session.execute(model.__table__.update().where(primary == identity).values(**changes))
        stored = await session.scalar(select(model.encrypted_content).where(primary == identity))
        if crypto.decrypt_values(row, stored, cipher) != content:
            raise crypto.DataKeyError("Veritabanındaki şifreli kopya doğrulanamadı")
        return 1


def _item_error(kind: str, exc: Exception) -> None:
    # İçerik, dosya yolu ve istisna mesajı loga taşınmaz.
    log.warning(
        "Şifreleme göçü öğesi atlandı; tekrar denenecek", kind=kind, error_type=type(exc).__name__
    )


async def migrate_content(batch_size: int = 100) -> dict[str, int]:
    """Her öğe ayrı işlemde; bozuk veya silinen öğe diğerlerini engellemez."""
    if batch_size < 1:
        raise ValueError("Göç grup boyutu pozitif olmalı")
    counts = {"rows": 0, "files": 0, "errors": 0}
    if not crypto.available():
        return counts
    for model in crypto.FIELDS:
        primary = model.meeting_id if model is crypto.WorkerJob else model.id
        last = None
        while True:
            async with session_scope() as session:
                query = select(primary).order_by(primary).limit(batch_size)
                if last is not None:
                    query = query.where(primary > last)
                ids = list(await session.scalars(query))
            if not ids:
                break
            for identity in ids:
                try:
                    counts["rows"] += await _migrate_row(model, identity)
                except Exception as exc:
                    counts["errors"] += 1
                    _item_error("row", exc)
            last = ids[-1]
            await asyncio.sleep(0)
    last = ""
    while True:
        async with session_scope() as session:
            meetings = list(
                await session.execute(
                    select(Meeting.id, Meeting.documents)
                    .where(Meeting.id > last)
                    .order_by(Meeting.id)
                    .limit(batch_size)
                )
            )
        if not meetings:
            break
        last = meetings[-1].id
        for meeting in meetings:
            try:
                await crypto.repair_orphan_marker(meeting.id)
            except Exception as exc:
                counts["errors"] += 1
                _item_error("marker", exc)
                continue
            files = {Path(v) for k, v in (meeting.documents or {}).items() if k in {"pdf", "docx"}}
            async with session_scope() as session:
                files.update(
                    Path(p)
                    for p in await session.scalars(
                        select(MeetingAsset.path).where(MeetingAsset.meeting_id == meeting.id)
                    )
                )
                has_key = await session.scalar(
                    select(MeetingDataKey.meeting_id).where(MeetingDataKey.meeting_id == meeting.id)
                )
            data = Path(get_settings().data_dir)
            for kind in ("reports", "screenshots", "presentations"):
                files.update((data / kind / meeting.id).glob("*"))
            files = {p for p in files if p.is_file() and not p.name.startswith(".katip-")}
            if not has_key and not files:
                continue
            try:
                cipher = await crypto.meeting_cipher(meeting.id, create=bool(files and not has_key))
            except Exception as exc:
                counts["errors"] += 1
                _item_error("key", exc)
                continue
            for path in sorted(files):
                try:
                    counts["files"] += await asyncio.to_thread(cipher.migrate_file, path)
                except Exception as exc:
                    counts["errors"] += 1
                    _item_error("file", exc)
        await asyncio.sleep(0)
    if not counts["errors"]:
        async with write_scope() as session:
            await session.execute(
                update(crypto.EncryptionInstallation).values(migration_completed=True)
            )
    return counts


_RETRY_S = 300


async def run_background() -> None:
    global state
    if not crypto.available():
        state = "kapalı"
        return
    while True:
        state = "çalışıyor"
        rotation_failed = False
        rotated = 0
        try:
            rotated = await rotate_keys()
        except Exception as exc:
            rotation_failed = True
            _item_error("rotation-cycle", exc)
        try:
            counts = await migrate_content()
            state = "başarısız" if counts["errors"] or rotation_failed else "tamamlandı"
            log.info("Toplantı şifreleme göçü turu tamamlandı", **counts, rotated=rotated)
        except Exception as exc:
            state = "başarısız"
            _item_error("cycle", exc)
        await asyncio.sleep(_RETRY_S)
