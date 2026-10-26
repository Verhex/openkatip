"""Depo işlemleri. Her fonksiyon kendi işlem sınırını (session_scope) açar."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import and_, delete, false, or_, select, true, update
from sqlalchemy.orm import selectinload

from katip.events import Segment
from katip.store import crypto
from katip.store.db import session_scope, write_scope
from katip.store.models import (
    ANONYMOUS_SPEAKER,
    AdminSession,
    AppSetting,
    AuditLog,
    Consent,
    Directive,
    Meeting,
    MeetingAsset,
    MeetingGrant,
    MeetingIdentity,
    MeetingMailDraft,
    MeetingNote,
    MeetingParticipant,
    MeetingSegment,
    MeetingTombstone,
    MeetingUsage,
    PanelAccount,
    Person,
    Role,
    TeamsConversation,
    Voiceprint,
)
from katip.voice import crypto as voice_crypto

# ------------------------------------------------------------------ toplantılar


def meeting_access_clause(account_id: int | None, role: str, permission: str = "read"):
    """Liste ve tekil sorgular aynı izin kuralını kullanır."""
    if role == "admin":
        return true()
    if account_id is None:
        return false()
    grants = select(MeetingGrant.meeting_id).where(MeetingGrant.account_id == account_id)
    if permission == "manage":
        if role != "operator":
            return false()
        grants = grants.where(MeetingGrant.permission == "manage")
    granted = Meeting.id.in_(grants)
    return or_(Meeting.owner_account_id == account_id, granted) if role == "operator" else granted


async def accessible_meeting_ids(
    account_id: int | None, role: str, permission: str = "read"
) -> set[str]:
    async with session_scope() as s:
        return set(
            await s.scalars(
                select(Meeting.id).where(meeting_access_clause(account_id, role, permission))
            )
        )


async def meeting_access(
    meeting_id: str, account_id: int | None, role: str, permission: str = "read"
) -> bool:
    if role == "admin":
        return True
    async with session_scope() as s:
        return (
            await s.scalar(
                select(Meeting.id).where(
                    Meeting.id == meeting_id, meeting_access_clause(account_id, role, permission)
                )
            )
            is not None
        )


async def list_meeting_grants(meeting_id: str) -> list[MeetingGrant]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(MeetingGrant)
                .where(MeetingGrant.meeting_id == meeting_id)
                .order_by(MeetingGrant.account_id)
            )
        )


async def set_meeting_grant(meeting_id: str, account_id: int, permission: str) -> None:
    async with write_scope() as s:
        row = await s.get(MeetingGrant, (meeting_id, account_id))
        if row is None:
            s.add(MeetingGrant(meeting_id=meeting_id, account_id=account_id, permission=permission))
        else:
            row.permission = permission


async def remove_meeting_grant(meeting_id: str, account_id: int) -> bool:
    async with write_scope() as s:
        result = await s.execute(
            delete(MeetingGrant).where(
                MeetingGrant.meeting_id == meeting_id, MeetingGrant.account_id == account_id
            )
        )
        return bool(result.rowcount)


async def _ensure_meeting(s, meeting_id: str) -> None:
    """Toplantı satırını yoksa oluşturur; eşzamanlı çağrılarda çakışmaz (ON CONFLICT DO NOTHING)."""
    await s.run_sync(lambda sync: crypto.assert_live(sync.connection(), meeting_id))
    await s.run_sync(lambda sync: crypto.require_write_key(sync.connection()))
    dialect = s.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover — desteklenen iki veritabanı dışında
        if await s.get(Meeting, meeting_id) is None:
            s.add(Meeting(id=meeting_id))
            await s.flush()
        return
    inserted = await s.execute(
        insert(Meeting).values(id=meeting_id).on_conflict_do_nothing(index_elements=["id"])
    )
    # PostgreSQL'de yardımcılar toplantı satırı üzerinden sıralanır.
    await s.execute(select(Meeting.id).where(Meeting.id == meeting_id).with_for_update())
    await s.run_sync(lambda sync: crypto.assert_live(sync.connection(), meeting_id))
    if crypto.available():
        cipher = await s.run_sync(
            lambda sync: crypto.cipher_for(sync.connection(), meeting_id, create=True)
        )
        if inserted.rowcount:
            row = Meeting(id=meeting_id)
            values = {f: None for f in crypto.FIELDS[Meeting]}
            await s.execute(
                Meeting.__table__.update()
                .where(Meeting.id == meeting_id)
                .values(
                    encrypted_content=crypto._encrypt_values(row, values, cipher),
                    report=None,
                )
            )


async def upsert_meeting(meeting_id: str, **fields) -> None:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        if fields:
            row = await s.get(Meeting, meeting_id)
            for name, value in fields.items():
                setattr(row, name, value)


async def save_segment(meeting_id: str, seg: Segment, person_id: int | None = None) -> None:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        row = await s.get(MeetingSegment, seg.id)
        values = {
            "meeting_id": meeting_id,
            "person_id": person_id,
            "speaker_name": seg.speaker,
            "speaker_label": seg.speaker_label,
            "speaker_email": seg.speaker_email,
            "start_s": seg.start_s,
            "end_s": seg.end_s,
            "text": seg.text,
            "translation": seg.translation,
            "lang": seg.lang,
        }
        if row is None:
            s.add(MeetingSegment(id=seg.id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)


async def finish_meeting(meeting_id: str, state: str) -> None:
    await upsert_meeting(meeting_id, state=state, ended_at=datetime.now(UTC))


async def meeting_minutes_today() -> float:
    """Bugün (UTC) başlayan toplantıların toplam süresi; devam edenler şu ana kadar sayılır."""
    from katip.config import get_settings

    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Çöken ve hiç kapanmayan bir kayıt günlük kotayı sonsuza dek doldurmasın.
    s = get_settings()
    cap_s = (max(s.max_meeting_minutes, s.record_max_minutes) + 5) * 60
    async with session_scope() as s:
        rows = await s.execute(
            select(Meeting.started_at, Meeting.ended_at).where(Meeting.started_at >= day_start)
        )
        total = 0.0
        for started, ended in rows.all():
            started = started if started.tzinfo else started.replace(tzinfo=UTC)
            end = ended or now
            end = end if end.tzinfo else end.replace(tzinfo=UTC)
            total += min(cap_s, max(0.0, (end - started).total_seconds()))
    return total / 60


async def purge_meetings_before(cutoff: datetime) -> tuple[list[str], list[str]]:
    """Başlangıcı cutoff'tan eski toplantıları siler; (silinen kimlikler, silinecek dosyalar)."""
    async with session_scope() as s:
        meetings = list(
            await s.execute(
                select(Meeting.id, Meeting.documents).where(Meeting.started_at < cutoff)
            )
        )
    from katip.reports.service import ReportPendingError

    ids = []
    files = []
    for meeting in meetings:
        try:
            async with session_scope() as s:
                paths = [v for k, v in (meeting.documents or {}).items() if k in {"pdf", "docx"}]
                paths += list(
                    await s.scalars(
                        select(MeetingAsset.path).where(MeetingAsset.meeting_id == meeting.id)
                    )
                )
            if await delete_meeting(meeting.id):
                ids.append(meeting.id)
                files.extend(paths)
        except ReportPendingError:
            continue  # Bekleyen batch sonraki periyotta tekrar değerlendirilir.
    return ids, files


async def delete_meeting(
    meeting_id: str, *, state: str | None = None, cancel_pending: bool = False
) -> bool:
    """Toplantı ve DEK zarfını birlikte siler; ardından dosyalarını kaldırır."""
    import asyncio

    from katip.config import get_settings
    from katip.reports.service import batch_is_pending, cancel_batch, stop_reports
    from katip.retention import _delete_meeting_files

    async with session_scope() as s:
        documents = await s.scalar(select(Meeting.documents).where(Meeting.id == meeting_id))
    stale = not batch_is_pending(documents)
    if cancel_pending and (documents or {}).get("batch"):
        await cancel_batch(documents["batch"])
    await stop_reports(meeting_id, cancel_pending=cancel_pending or stale)
    if cancel_pending:
        await update_batch_marker(meeting_id, {})
    # Kilit thread'de beklenir; render ve göç aynı kilidi kullanır.
    guard = crypto.file_lock(meeting_id, deleting=True)
    acquiring = asyncio.create_task(asyncio.to_thread(guard.__enter__))
    try:
        stream = await asyncio.shield(acquiring)
    except asyncio.CancelledError:
        await acquiring
        await asyncio.to_thread(guard.__exit__, None, None, None)
        raise
    try:
        return await _delete_locked(meeting_id, state, stream, _delete_meeting_files, get_settings)
    finally:
        await asyncio.to_thread(guard.__exit__, None, None, None)
        await asyncio.to_thread(crypto.cleanup_lock, meeting_id)


async def _delete_locked(meeting_id, state, stream, delete_files, settings) -> bool:
    import asyncio
    from pathlib import Path

    from katip.reports.service import ReportPendingError, batch_is_pending

    async with write_scope() as s:
        query = select(Meeting.documents).where(Meeting.id == meeting_id).with_for_update()
        if state is not None:
            query = query.where(Meeting.state == state)
        documents = (await s.execute(query)).first()
        if documents is None:
            return False
        sending = await s.scalar(
            select(MeetingMailDraft.id).where(
                MeetingMailDraft.meeting_id == meeting_id,
                MeetingMailDraft.status == "gönderiliyor",
                MeetingMailDraft.error.is_(None),
                MeetingMailDraft.attempted_at > datetime.now(UTC) - timedelta(minutes=3),
            )
        )
        if sending:
            raise ReportPendingError("Mail gönderimi sürüyor; tamamlanınca tekrar deneyin")
        if batch_is_pending(documents[0]):
            raise ReportPendingError("Rapor batch'i bekliyor; tamamlanınca içeriği silebilirsiniz")
        files = [v for k, v in (documents[0] or {}).items() if k in {"pdf", "docx"}]
        files += list(
            await s.scalars(select(MeetingAsset.path).where(MeetingAsset.meeting_id == meeting_id))
        )
        deletion = delete(Meeting).where(Meeting.id == meeting_id)
        if state is not None:
            deletion = deletion.where(Meeting.state == state)
        result = await s.execute(deletion)
        if not result.rowcount:
            return False
        await s.run_sync(
            lambda sync: crypto._insert(
                sync.connection(),
                MeetingTombstone.__table__,
                {"meeting_id": meeting_id, "deleted_at": datetime.now(UTC)},
                "meeting_id",
            )
        )
    # Yalnız başarılı commit'ten sonra, dosya kilidi hâlâ eldeyken işaretle.
    crypto.mark_deleted(meeting_id, stream)
    await asyncio.to_thread(delete_files, Path(settings().data_dir), [meeting_id], files)
    return True


async def meetings_missing_report(min_segments: int = 3, limit: int = 20) -> list[str]:
    """Raporu hiç üretilmemiş ama yeterli konuşması olan toplantılar (ör. sunucu yeniden
    başladığı için yarıda kalanlar)."""
    from sqlalchemy import func

    async with session_scope() as s:
        rows = await s.execute(
            select(Meeting.id)
            .join(MeetingSegment, MeetingSegment.meeting_id == Meeting.id)
            .where(Meeting.report.is_(None))
            .group_by(Meeting.id)
            .having(func.count(MeetingSegment.id) >= min_segments)
            .limit(limit)
        )
        return [r[0] for r in rows.all()]


async def close_interrupted(meeting_id: str) -> None:
    """Kapanmadan kalmış toplantıyı, son konuşmanın bittiği ana göre kapatır."""
    from datetime import timedelta

    from sqlalchemy import func

    async with write_scope() as s:
        meeting = await s.get(Meeting, meeting_id)
        if meeting is None or meeting.ended_at is not None:
            return
        last = await s.scalar(
            select(func.max(MeetingSegment.end_s)).where(MeetingSegment.meeting_id == meeting_id)
        )
        started = (
            meeting.started_at
            if meeting.started_at.tzinfo
            else meeting.started_at.replace(tzinfo=UTC)
        )
        meeting.ended_at = started + timedelta(seconds=float(last or 0))
        meeting.state = "interrupted"


async def get_meeting(meeting_id: str) -> Meeting | None:
    async with session_scope() as s:
        return await s.scalar(
            select(Meeting).where(Meeting.id == meeting_id).options(selectinload(Meeting.segments))
        )


async def set_report(meeting_id: str, report: dict, documents: dict[str, str]) -> None:
    await upsert_meeting(meeting_id, report=report, documents=documents)


async def set_documents(meeting_id: str, documents: dict[str, str]) -> None:
    async with write_scope() as s:
        await s.run_sync(lambda sync: crypto.assert_live(sync.connection(), meeting_id))
        meeting = await s.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
        if meeting is not None:
            meeting.documents = documents


async def update_batch_marker(meeting_id: str, marker: dict[str, str]) -> None:
    """Rapor yollarını korur; batch alanlarını tek işlemde değiştirir."""
    async with write_scope() as s:
        meeting = await s.scalar(select(Meeting).where(Meeting.id == meeting_id).with_for_update())
        if meeting is not None:
            documents = dict(meeting.documents or {})
            for key in ("batch", "batch_pending", "batch_started_at"):
                documents.pop(key, None)
            documents.update(marker)
            meeting.documents = documents


async def set_segment_person(segment_ids: list[str], person_id: int, name: str) -> None:
    """Ses kimliği sonradan oturunca geçmiş parçaları kişiye bağlar."""
    if not segment_ids:
        return
    async with write_scope() as s:
        for row in await s.scalars(
            select(MeetingSegment).where(MeetingSegment.id.in_(segment_ids)).with_for_update()
        ):
            row.person_id, row.speaker_name = person_id, name


async def add_note(meeting_id: str, kind: str, text: str, author: str | None) -> int:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        note = MeetingNote(meeting_id=meeting_id, kind=kind, text=text, author=author)
        s.add(note)
        await s.flush()
        return note.id


async def update_note(meeting_id: str, note_id: int, text: str, kind: str | None = None) -> bool:
    async with write_scope() as s:
        note = await s.get(MeetingNote, note_id)
        if note is None or note.meeting_id != meeting_id:
            return False
        note.text = text
        if kind:
            note.kind = kind
        return True


async def delete_note(meeting_id: str, note_id: int) -> bool:
    async with write_scope() as s:
        note = await s.get(MeetingNote, note_id)
        if note is None or note.meeting_id != meeting_id:
            return False
        await s.delete(note)
        return True


async def upsert_participant(
    meeting_id: str,
    name: str,
    person_id: int | None,
    email: str | None,
    *,
    platform_id: str | None = None,
    identity_source: str = "unknown",
) -> None:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        lookup = await crypto.lookup_values(s, MeetingParticipant, meeting_id, "name", name)
        row = await s.scalar(
            select(MeetingParticipant).where(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.name.in_(lookup),
            )
        )
        if row is None:
            s.add(
                MeetingParticipant(
                    meeting_id=meeting_id,
                    name=name,
                    person_id=person_id,
                    email=email,
                    platform_id=platform_id,
                    identity_source=identity_source,
                )
            )
        else:
            row.person_id = person_id
            row.email = email
            row.platform_id = platform_id
            row.identity_source = identity_source


async def save_meeting_identity(meeting_id: str, identity, source: str) -> None:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        lookup = await crypto.lookup_values(s, MeetingIdentity, meeting_id, "email", identity.email)
        row = await s.scalar(
            select(MeetingIdentity).where(
                MeetingIdentity.meeting_id == meeting_id,
                MeetingIdentity.email.in_(lookup),
            )
        )
        if row is None:
            s.add(MeetingIdentity(meeting_id=meeting_id, **identity.model_dump(), source=source))
        else:
            row.name, row.platform_id, row.source = identity.name, identity.platform_id, source


async def list_meeting_identities(meeting_id: str) -> list[MeetingIdentity]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(MeetingIdentity)
                .where(MeetingIdentity.meeting_id == meeting_id)
                .order_by(MeetingIdentity.id)
            )
        )


async def bind_participant(meeting_id: str, name: str, identity, source: str) -> Person:
    """Yalnız e-postayla kişi oluştur/eşleştir; ada göre başka hesaba birleştirme yok."""
    person = await ensure_person_by_email(identity.email, identity.name)
    prior = next((p for p in await list_participants(meeting_id) if p.name == name), None)
    changed = prior is None or prior.email != identity.email
    await upsert_participant(
        meeting_id,
        name,
        person.id,
        identity.email,
        platform_id=identity.platform_id,
        identity_source=source,
    )
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        for row in await s.scalars(
            select(MeetingSegment).where(MeetingSegment.meeting_id == meeting_id)
        ):
            if row.speaker_name == name:
                row.person_id, row.speaker_email = person.id, identity.email
        # Önceki belgede eski kimlik kalmasın; kullanıcı raporu yeniden oluşturabilir.
        meeting = await s.get(Meeting, meeting_id)
        if changed and meeting is not None and meeting.documents:
            meeting.documents = {}
    return person


async def list_participants(meeting_id: str) -> list[MeetingParticipant]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(MeetingParticipant)
                .where(MeetingParticipant.meeting_id == meeting_id)
                .order_by(MeetingParticipant.first_seen)
            )
        )


_USAGE_FIELDS = (
    "llm_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "llm_usd",
    "stt_seconds",
    "stt_usd",
    "tts_seconds",
    "tts_usd",
)


async def add_usage(meeting_id: str, *, provider_key: str | None = None, **deltas) -> None:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        row = await s.get(MeetingUsage, meeting_id)
        if row is None:
            row = MeetingUsage(meeting_id=meeting_id, **{f: 0 for f in _USAGE_FIELDS})
            s.add(row)
        for field, value in deltas.items():
            if field in _USAGE_FIELDS:
                setattr(row, field, (getattr(row, field) or 0) + value)
        if provider_key:
            providers = dict(row.providers or {})
            totals = dict(providers.get(provider_key, {}))
            for field, value in deltas.items():
                if field in _USAGE_FIELDS:
                    totals[field] = totals.get(field, 0) + value
            providers[provider_key] = totals
            row.providers = providers
        row.updated_at = datetime.now(UTC)


async def usage_report(
    days: int = 30, *, account_id: int | None = None, role: str = "admin"
) -> list[tuple[Meeting, MeetingUsage]]:
    from datetime import timedelta

    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as s:
        rows = await s.execute(
            select(Meeting, MeetingUsage)
            .join(MeetingUsage, MeetingUsage.meeting_id == Meeting.id)
            .where(Meeting.started_at >= since)
            .where(meeting_access_clause(account_id, role))
            .order_by(Meeting.started_at.desc())
        )
        return [(m, u) for m, u in rows.all()]


async def get_meeting_usage(meeting_id: str) -> MeetingUsage | None:
    async with session_scope() as s:
        return await s.get(MeetingUsage, meeting_id)


async def list_notes(meeting_id: str) -> list[MeetingNote]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(MeetingNote)
                .where(MeetingNote.meeting_id == meeting_id)
                .order_by(MeetingNote.at)
            )
        )


async def add_asset(
    meeting_id: str, kind: str, number: int, path: str, at_s: float, requested_by: str | None
) -> int:
    async with write_scope() as s:
        await _ensure_meeting(s, meeting_id)
        row = MeetingAsset(
            meeting_id=meeting_id,
            kind=kind,
            number=number,
            path=path,
            at_s=at_s,
            requested_by=requested_by,
        )
        s.add(row)
        await s.flush()
        return row.id


async def set_asset_caption(asset_id: int, caption: str) -> None:
    async with write_scope() as s:
        row = await s.get(MeetingAsset, asset_id)
        if row is not None:
            row.caption = caption


async def list_assets(meeting_id: str, kind: str = "screenshot") -> list[MeetingAsset]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(MeetingAsset)
                .where(MeetingAsset.meeting_id == meeting_id, MeetingAsset.kind == kind)
                .order_by(MeetingAsset.number)
            )
        )


async def recent_reports(
    limit: int = 5,
    exclude: str | None = None,
    *,
    reader_emails: set[str] | None = None,
    scope_meeting_id: str | None = None,
) -> list[Meeting]:
    """Agent'ın "geçen toplantıda ne demiştik?" sorularına bağlam."""
    async with session_scope() as s:
        query = (
            select(Meeting).where(Meeting.report.is_not(None)).order_by(Meeting.started_at.desc())
        )
        if exclude:
            query = query.where(Meeting.id != exclude)
        if reader_emails is not None:
            accounts = list(
                await s.scalars(
                    select(PanelAccount).where(
                        PanelAccount.email.in_(reader_emails), PanelAccount.enabled.is_(True)
                    )
                )
            )
            if not accounts or len(accounts) != len(reader_emails):
                return []
            query = query.where(and_(*(meeting_access_clause(a.id, a.role) for a in accounts)))
        if scope_meeting_id is not None:
            current = await s.get(Meeting, scope_meeting_id)
            if current is None or current.owner_account_id is None:
                return []
            owner = await s.get(PanelAccount, current.owner_account_id)
            if owner is None or not owner.enabled:
                return []
            # Katılımcı beyanı toplantı sahibinin erişimini genişletemez.
            query = query.where(meeting_access_clause(owner.id, owner.role))
        return list(await s.scalars(query.limit(limit)))


async def add_directive(text: str, meeting_id: str | None) -> int:
    async with write_scope() as s:
        row = Directive(text=text, meeting_id=meeting_id)
        s.add(row)
        await s.flush()
        s.add(AuditLog(actor="admin", action="directive", detail={"id": row.id, "text": text}))
        return row.id


async def list_directives() -> list[str]:
    async with session_scope() as s:
        rows = await s.scalars(
            select(Directive).where(Directive.active.is_(True)).order_by(Directive.id)
        )
        return [d.text for d in rows]


# ----------------------------------------------------------------------- kişiler


async def list_people() -> list[Person]:
    async with session_scope() as s:
        return list(await s.scalars(select(Person).order_by(Person.display_name)))


async def get_person(person_id: int) -> Person | None:
    async with session_scope() as s:
        return await s.get(Person, person_id)


async def find_person_by_name(name: str) -> Person | None:
    for person in await list_people():
        if person.matches_name(name):
            return person
    return None


async def ensure_person(display_name: str) -> Person:
    """Adıyla bul; yoksa (şemada olmayan biri ses kaydı isterse) üye olarak oluştur."""
    if found := await find_person_by_name(display_name):
        return found
    async with write_scope() as s:
        person = Person(display_name=display_name, role=Role.MEMBER, aliases=[])
        s.add(person)
        await s.flush()
        return person


async def ensure_person_by_email(email: str, name: str) -> Person:
    from katip.identity import normalize_email

    email = normalize_email(email)
    async with write_scope() as s:
        person = await s.scalar(select(Person).where(Person.email == email))
        if person is None:
            # İsim aynı olsa bile e-postası farklı kişiler ayrı profildir.
            dialect = s.bind.dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            await s.execute(
                insert(Person)
                .values(email=email, display_name=name, role=Role.MEMBER, aliases=[])
                .on_conflict_do_nothing(index_elements=["email"])
            )
            person = await s.scalar(select(Person).where(Person.email == email))
        assert person is not None
        return person


async def add_consent(person_id: int, kind: str, evidence: str, meeting_id: str | None) -> None:
    async with write_scope() as s:
        s.add(Consent(person_id=person_id, kind=kind, evidence=evidence, meeting_id=meeting_id))
        s.add(
            AuditLog(
                actor=_actor(person_id),
                action=f"consent:{kind}",
                detail={"meeting_id": meeting_id, "evidence": evidence},
            )
        )


async def has_consent(person_id: int, kind: str) -> bool:
    async with session_scope() as s:
        found = await s.scalar(
            select(Consent.id).where(Consent.person_id == person_id, Consent.kind == kind)
        )
        return found is not None


# -------------------------------------------------------------------- ses izleri


async def save_voiceprint(
    person_id: int, embedding: np.ndarray, *, model: str, speech_seconds: float, samples: int
) -> None:
    if not await has_consent(person_id, "voiceprint"):
        raise PermissionError("ses izi için açık rıza kaydı yok")
    vec = np.asarray(embedding, dtype=np.float32)
    # Biyometrik veri yalnızca şifreli yazılır; anahtar yoksa burada hata verir.
    blob = voice_crypto.encrypt(vec.tobytes(), person_id=person_id, model=model)
    async with write_scope() as s:
        row = await s.scalar(select(Voiceprint).where(Voiceprint.person_id == person_id))
        if row is None:
            row = Voiceprint(person_id=person_id)
            s.add(row)
        row.embedding = blob
        row.dim = int(vec.shape[0])
        row.model = model
        row.speech_seconds = speech_seconds
        row.samples = samples


async def load_voiceprints(model: str) -> list[tuple[int, str, str, np.ndarray]]:
    """(person_id, ad, rol, vektör) — yalnızca verilen modelle üretilmiş izler.

    Şifreleme öncesinden kalan düz izler bulunursa yerinde şifrelenir (tek seferlik göç).
    Anahtar yoksa hiçbir iz yüklenmez.
    """
    out = []
    async with session_scope() as s:
        rows = await s.execute(
            select(Voiceprint, Person).join(Person).where(Voiceprint.model == model)
        )
        for v, p in rows.all():
            if voice_crypto.is_encrypted(v.embedding):
                raw = voice_crypto.decrypt(v.embedding, person_id=p.id, model=v.model)
            else:
                raw = v.embedding
                v.embedding = voice_crypto.encrypt(raw, person_id=p.id, model=v.model)
            out.append((p.id, p.display_name, p.role, np.frombuffer(raw, dtype=np.float32).copy()))
    return out


async def enrolled_person_ids() -> set[int]:
    async with session_scope() as s:
        return set(await s.scalars(select(Voiceprint.person_id)))


# ------------------------------------------------------------------ beni unut


def _actor(person_id: int) -> str:
    # Denetim kaydında kişisel veri tutmamak için tek yönlü özet.
    return "person:" + hashlib.sha256(f"katip-person-{person_id}".encode()).hexdigest()[:16]


def _scrub(value, names: list[str]):
    """JSON içindeki tüm metinlerde kişinin adlarını anonim etiketle değiştirir."""
    if isinstance(value, str):
        for name in names:
            value = value.replace(name, ANONYMOUS_SPEAKER)
        return value
    if isinstance(value, list):
        return [_scrub(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, names) for k, v in value.items()}
    return value


async def _erase_meeting_person(
    mid, person_id, names, email, batch_size=100
) -> tuple[int, int, list[str]]:
    import asyncio
    from pathlib import Path

    from katip.documents.service import erase_presentations

    async with write_scope() as s:
        await s.scalar(select(Meeting.id).where(Meeting.id == mid).with_for_update())
        sending = await s.scalar(
            select(MeetingMailDraft.id).where(
                MeetingMailDraft.meeting_id == mid,
                MeetingMailDraft.status == "gönderiliyor",
                MeetingMailDraft.error.is_(None),
                MeetingMailDraft.attempted_at > datetime.now(UTC) - timedelta(minutes=3),
            )
        )
        if sending:
            raise RuntimeError("Mail gönderimi sürüyor")
        # Serbest metin ve alıcı kopyaları kişi silme işleminde yeniden kullanılmaz.
        await s.execute(delete(MeetingMailDraft).where(MeetingMailDraft.meeting_id == mid))
        await s.execute(delete(TeamsConversation).where(TeamsConversation.meeting_id == mid))

    segments = 0
    associated = False
    for model, name_field, email_field in (
        (MeetingSegment, "speaker_name", "speaker_email"),
        (MeetingParticipant, "name", "email"),
        (MeetingIdentity, "name", "email"),
    ):
        last = None
        while True:
            async with write_scope() as s:
                terms = []
                if hasattr(model, "person_id"):
                    terms.append(model.person_id == person_id)
                if model is not MeetingIdentity:
                    name_values = []
                    for name in names:
                        name_values += await crypto.lookup_values(s, model, mid, name_field, name)
                    name_match = getattr(model, name_field).in_(name_values)
                    name_match = name_match & model.person_id.is_(None)
                    terms.append(name_match)
                if model is MeetingSegment:
                    # v1'de bağlı olmayan konuşmacı adı için arama özeti yoktu.
                    terms.append(
                        model.person_id.is_(None)
                        & (model.speaker_name == "")
                        & model.encrypted_content.is_not(None)
                    )
                if email:
                    email_values = await crypto.lookup_values(s, model, mid, email_field, email)
                    terms.append(getattr(model, email_field).in_(email_values))
                query = select(model).where(model.meeting_id == mid, or_(false(), *terms))
                if last is not None:
                    query = query.where(model.id > last)
                rows = list(
                    await s.scalars(query.order_by(model.id).limit(batch_size).with_for_update())
                )
                if not rows:
                    break
                last = rows[-1].id
                for row in rows:
                    if model is not MeetingIdentity and not (
                        row.person_id == person_id
                        or (row.person_id is None and getattr(row, name_field) in names)
                        or (email and getattr(row, email_field) == email)
                    ):
                        continue
                    if model is MeetingSegment:
                        row.person_id = None
                        row.speaker_name = ANONYMOUS_SPEAKER
                        row.speaker_email = None
                        segments += 1
                    else:
                        await s.delete(row)
                    associated = True
    await erase_presentations(mid, names + ([email] if email else []), associated=associated)
    stale = []
    reports = 0
    async with write_scope() as s:
        # Raporun serbest metnindeki bir anma katılımcı kaydıyla sınırlanamaz.
        # Yalnız raporu olan toplantı okunur; bu işlem segment/belge koleksiyonlarını yüklemez.
        meeting = await s.scalar(
            select(Meeting).where(Meeting.id == mid, Meeting.report.is_not(None)).with_for_update()
        )
        if meeting and meeting.report:
            scrubbed = _scrub(meeting.report, names + ([email] if email else []))
            if scrubbed != meeting.report:
                stale = [v for k, v in (meeting.documents or {}).items() if k in {"pdf", "docx"}]
                # Dosya silinemiyorsa rapor ve yolları korunur; sonraki tur tekrar dener.
                for path in stale:
                    await asyncio.to_thread(Path(path).unlink, missing_ok=True)
                meeting.report = scrubbed
                meeting.documents = {}
                reports = 1
    return segments, reports, stale


async def forget_person(
    person_id: int, *, requested_by: str, failures: list[str] | None = None
) -> bool:
    """Küçük işlemlerle anonimleştirir; hata varsa kimlik ve silme talebi tekrar için korunur."""
    from katip.logging import get_logger

    async with write_scope() as s:
        person = await s.get(Person, person_id)
        if person is None:
            return False
        person.erasure_pending = True
        # Biyometrik veriyi başarısız toplantıların yeniden denenmesini bekletmeden sil.
        await s.execute(delete(Voiceprint).where(Voiceprint.person_id == person_id))
        names = sorted({person.display_name, *(person.aliases or [])}, key=len, reverse=True)
        email = person.email
    segments = reports = 0
    stale_files = []
    failed = []
    last = ""
    while True:
        async with session_scope() as s:
            ids = list(
                await s.scalars(
                    select(Meeting.id).where(Meeting.id > last).order_by(Meeting.id).limit(100)
                )
            )
        if not ids:
            break
        for mid in ids:
            try:
                count, scrubbed, files = await _erase_meeting_person(mid, person_id, names, email)
                segments += count
                reports += scrubbed
                stale_files += files
            except Exception as exc:
                failed.append(hashlib.sha256(mid.encode()).hexdigest()[:16])
                get_logger(__name__).warning(
                    "Kişi silmede toplantı işlenemedi",
                    meeting_hash=failed[-1],
                    error_type=type(exc).__name__,
                )
        last = ids[-1]
    async with write_scope() as s:
        await s.execute(
            update(Person).where(Person.manager_id == person_id).values(manager_id=None)
        )
        person = await s.get(Person, person_id)
        if person is not None and not failed:
            await s.delete(person)
        s.add(
            AuditLog(
                actor=_actor(person_id),
                action="forget",
                detail={
                    "requested_by": requested_by,
                    "segments_anonymized": segments,
                    "reports_scrubbed": reports,
                    "files_deleted": len(stale_files),
                    "failed_meetings": failed,
                },
            )
        )
    if failures is not None:
        failures.extend(failed)
    return not failed


async def pending_erasure_ids() -> list[int]:
    async with session_scope() as s:
        return list(await s.scalars(select(Person.id).where(Person.erasure_pending.is_(True))))


async def retry_erasures() -> None:
    """Kalıcı silme talepleri süreç yeniden başlasa da beş dakikada bir denenir."""
    import asyncio

    from katip.logging import get_logger

    while True:
        try:
            for person_id in await pending_erasure_ids():
                await forget_person(person_id, requested_by="retry")
        except Exception as exc:
            get_logger(__name__).warning(
                "Silme yeniden denemesi başarısız", error_type=type(exc).__name__
            )
        await asyncio.sleep(300)


# ------------------------------------------------------------------ yönetim paneli


async def create_admin_session(
    token_hash: str,
    csrf_hash: str,
    expires_at: datetime,
    client: str | None,
    user_agent: str | None,
    account_id: int | None = None,
) -> None:
    async with write_scope() as s:
        s.add(
            AdminSession(
                token_hash=token_hash,
                csrf_hash=csrf_hash,
                expires_at=expires_at,
                client=client,
                user_agent=(user_agent or "")[:200] or None,
                account_id=account_id,
            )
        )


async def get_admin_session(token_hash: str) -> AdminSession | None:
    async with session_scope() as s:
        return await s.scalar(select(AdminSession).where(AdminSession.token_hash == token_hash))


async def list_panel_accounts() -> list[PanelAccount]:
    async with session_scope() as s:
        return list(await s.scalars(select(PanelAccount).order_by(PanelAccount.email)))


async def get_panel_account(account_id: int) -> PanelAccount | None:
    async with session_scope() as s:
        return await s.get(PanelAccount, account_id)


async def find_panel_account(email: str) -> PanelAccount | None:
    async with session_scope() as s:
        return await s.scalar(select(PanelAccount).where(PanelAccount.email == email))


async def set_panel_account(
    email: str, password_hash: str, enabled: bool = True, role: str | None = None
) -> PanelAccount:
    if role is not None and role not in {"admin", "operator", "viewer"}:
        raise ValueError("Geçersiz panel rolü")
    async with write_scope() as s:
        account = await s.scalar(select(PanelAccount).where(PanelAccount.email == email))
        if account is None:
            account = PanelAccount(
                email=email, password_hash=password_hash, enabled=enabled, role=role or "operator"
            )
            s.add(account)
        else:
            account.password_hash, account.enabled = password_hash, enabled
            if role is not None:
                account.role = role
            await s.execute(delete(AdminSession).where(AdminSession.account_id == account.id))
        await s.flush()
        return account


async def disable_panel_account(account_id: int) -> bool:
    async with write_scope() as s:
        account = await s.get(PanelAccount, account_id)
        if account is None:
            return False
        account.enabled = False
        await s.execute(delete(AdminSession).where(AdminSession.account_id == account_id))
        return True


async def update_panel_account(
    account_id: int,
    *,
    role: str | None = None,
    enabled: bool | None = None,
    password_hash: str | None = None,
    actor_account_id: int | None = None,
    env_admin: bool = False,
    deleting: bool = False,
) -> PanelAccount:
    """Son yönetici denetimi ve oturum iptali aynı işlemde yapılır."""
    from fastapi import HTTPException

    async with write_scope() as s:
        accounts = list(
            await s.scalars(select(PanelAccount).order_by(PanelAccount.id).with_for_update())
        )
        account = next((a for a in accounts if a.id == account_id), None)
        if account is None:
            raise HTTPException(404, "Hesap bulunamadı")
        if actor_account_id == account_id and (
            role is not None and role != account.role or enabled is False
        ):
            raise HTTPException(403, "Kendi rolünüzü değiştiremez veya hesabınızı kapatamazsınız")
        next_role = role if role is not None else account.role
        next_enabled = enabled if enabled is not None else account.enabled
        if not env_admin and not any(
            (next_enabled and next_role == "admin")
            if a.id == account_id
            else (a.enabled and a.role == "admin")
            for a in accounts
        ):
            raise HTTPException(409, "En az bir açık yönetici hesabı bulunmalı")
        detail = {"account_id": account_id}
        actor = f"panel:{actor_account_id}" if actor_account_id else "admin"
        if account.role != next_role:
            await audit(
                actor,
                "panel_account_role_changed",
                transaction=s,
                **detail,
                old_role=account.role,
                role=next_role,
            )
        if account.enabled != next_enabled:
            await audit(
                actor,
                "panel_account_enabled" if next_enabled else "panel_account_disabled",
                transaction=s,
                **detail,
            )
        if deleting:
            await audit(actor, "panel_account_deleted", transaction=s, **detail)
        if account.role != next_role or account.enabled != next_enabled or password_hash:
            await s.execute(delete(AdminSession).where(AdminSession.account_id == account_id))
        account.role, account.enabled = next_role, next_enabled
        if password_hash:
            account.password_hash = password_hash
        await s.flush()
        return account


async def touch_admin_session(session_id: int) -> None:
    async with write_scope() as s:
        row = await s.get(AdminSession, session_id)
        if row is not None:
            row.last_seen = datetime.now(UTC)


async def change_panel_password(account_id: int, password_hash: str) -> None:
    from fastapi import HTTPException

    async with write_scope() as s:
        account = await s.get(PanelAccount, account_id, with_for_update=True)
        if account is None or not account.enabled:
            raise HTTPException(401, "Oturum yok")
        account.password_hash = password_hash
        await s.execute(delete(AdminSession).where(AdminSession.account_id == account_id))


async def delete_admin_session(token_hash: str) -> None:
    async with write_scope() as s:
        await s.execute(delete(AdminSession).where(AdminSession.token_hash == token_hash))


async def delete_all_admin_sessions() -> int:
    async with write_scope() as s:
        result = await s.execute(delete(AdminSession))
        return result.rowcount or 0


async def list_admin_sessions() -> list[AdminSession]:
    async with session_scope() as s:
        return list(await s.scalars(select(AdminSession).order_by(AdminSession.last_seen.desc())))


async def purge_expired_admin_sessions() -> None:
    async with write_scope() as s:
        await s.execute(delete(AdminSession).where(AdminSession.expires_at < datetime.now(UTC)))


async def get_app_settings() -> dict[str, object]:
    async with session_scope() as s:
        rows = await s.scalars(select(AppSetting))
        return {r.key: (r.value or {}).get("v") for r in rows}


async def set_app_settings(values: dict[str, object]) -> None:
    async with write_scope() as s:
        for key, value in values.items():
            row = await s.get(AppSetting, key)
            if row is None:
                s.add(AppSetting(key=key, value={"v": value}))
            else:
                row.value = {"v": value}
                row.updated_at = datetime.now(UTC)


async def recent_audit(actions: tuple[str, ...], limit: int = 30) -> list[AuditLog]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(AuditLog)
                .where(AuditLog.action.in_(actions))
                .order_by(AuditLog.id.desc())
                .limit(limit)
            )
        )


async def recent_meetings(
    limit: int = 30, *, account_id: int | None = None, role: str = "admin"
) -> list[Meeting]:
    async with session_scope() as s:
        return list(
            await s.scalars(
                select(Meeting)
                .where(meeting_access_clause(account_id, role))
                .order_by(Meeting.started_at.desc())
                .limit(limit)
            )
        )


async def audit(actor: str, action: str, *, transaction=None, **detail) -> None:
    if transaction is not None:
        transaction.add(AuditLog(actor=actor, action=action, detail=detail))
        return
    async with write_scope() as s:
        s.add(AuditLog(actor=actor, action=action, detail=detail))
