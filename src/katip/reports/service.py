"""Toplantı sonu raporu: depodan transkripti al → Claude özeti → DOCX + PDF → depoya yaz."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

from katip import usage
from katip.async_utils import LoopLocal, wait_tasks
from katip.config import get_settings
from katip.logging import get_logger
from katip.reports.render import ReportContext, render_docx, render_pdf
from katip.reports.summarize import MeetingReport, summarize_meeting
from katip.store import repo
from katip.store.crypto import DataKeyError, meeting_cipher
from katip.store.models import KATIP_SPEAKER

log = get_logger(__name__)

_MIN_SEGMENTS = 3  # birkaç cümlelik "test" toplantıları için rapor üretme
_tasks: LoopLocal[dict[str, dict[asyncio.Task, bool]]] = LoopLocal(dict)
_STOP_TIMEOUT_S = 2.0


class ReportPendingError(RuntimeError):
    """Güncel batch, otomatik silme işlemini bekletir; yönetici iptal edebilir."""


def track_report(meeting_id: str, task: asyncio.Task, *, batch: bool = False) -> None:
    tracked = _tasks.get()
    tasks = tracked.setdefault(meeting_id, {})
    tasks[task] = batch

    def finished(done):
        tasks.pop(done, None)
        if not tasks and tracked.get(meeting_id) is tasks:
            tracked.pop(meeting_id, None)

    task.add_done_callback(finished)


def batch_is_pending(documents: dict | None) -> bool:
    """Eski veya tarihsiz batch işaretleri silmeyi süresiz engellemez."""
    documents = documents or {}
    if not (documents.get("batch") or documents.get("batch_pending")):
        return False
    try:
        started = datetime.fromisoformat(documents["batch_started_at"])
        started = started.replace(tzinfo=UTC) if started.tzinfo is None else started
    except (KeyError, TypeError, ValueError):
        return False
    settings = get_settings()
    margin = settings.report_deadline_s + 60
    return (datetime.now(UTC) - started).total_seconds() < settings.report_batch_max_wait_s + margin


async def cancel_batch(batch_id: str) -> None:
    from katip.providers.registry import get_registry

    client = get_registry().llm_provider("anthropic").client
    async with asyncio.timeout(get_settings().report_deadline_s):
        await client.messages.batches.cancel(batch_id)


async def stop_reports(meeting_id: str, *, cancel_pending: bool = False) -> None:
    tasks = dict(_tasks.get().get(meeting_id, {}))
    if any(tasks.values()) and not cancel_pending:
        raise ReportPendingError("Rapor batch'i bekliyor; tamamlanınca içeriği silebilirsiniz")
    pending = await wait_tasks(tasks, budget_s=_STOP_TIMEOUT_S, label="rapor", cancel=True)
    if pending:
        # Hâlâ yazabilecek bir rapor varken silme işlemini başlatma.
        raise TimeoutError("Rapor görevleri kapanmadı; silme işlemi yeniden denenmeli")


async def generate_report(meeting_id: str, **kwargs) -> dict[str, str]:
    task = asyncio.create_task(_generate_report(meeting_id, **kwargs))
    track_report(meeting_id, task, batch=kwargs.get("batch", False))
    return await task


async def shutdown_reports() -> None:
    tasks = {task for group in _tasks.get().values() for task in group}
    await wait_tasks(tasks, budget_s=_STOP_TIMEOUT_S, label="uygulama raporları", cancel=True)


async def _participants(
    meeting_id: str, segments: list[dict]
) -> list[tuple[str, str | None, str | None]]:
    """Toplantıda görünen herkes + konuşanlar: (ad, unvan, e-posta). Katip ve etiketler hariç."""
    participants = await repo.list_participants(meeting_id)
    by_name = {p.name: p for p in participants}
    seen = [p.name for p in participants]
    seen += [s["speaker_name"] for s in segments if s.get("speaker_name")]
    names = [
        n for n in dict.fromkeys(seen) if n != KATIP_SPEAKER and not n.startswith("Konuşmacı ")
    ]
    people = await repo.list_people()
    out: dict[str, tuple[str, str | None, str | None]] = {}
    for name in names:
        participant = by_name.get(name)
        person = next((p for p in people if participant and p.id == participant.person_id), None)
        key = person.display_name if person else name
        out.setdefault(
            (participant.email if participant else None) or name,
            (key, person.title if person else None, participant.email if participant else None),
        )
    return list(out.values())


async def _generate_report(
    meeting_id: str,
    *,
    summarizer=summarize_meeting,
    resummarize: bool = False,
    batch: bool = False,
    interim: bool = False,
) -> dict[str, str]:
    """Raporu üretir ve belge yollarını döner: {"pdf": ..., "docx": ...}."""
    settings = get_settings()
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        raise LookupError(f"toplantı bulunamadı: {meeting_id}")

    segments = [
        {
            "start_s": s.start_s,
            "speaker_name": s.speaker_name,
            "speaker_email": s.speaker_email,
            "text": s.text,
            "translation": s.translation,
        }
        for s in meeting.segments
    ]
    if len(segments) < _MIN_SEGMENTS:
        log.info("rapor atlandı: yetersiz konuşma", meeting_id=meeting_id, segments=len(segments))
        return {}

    labels = {"note": "Not", "action": "Aksiyon", "decision": "Karar"}
    notes = await repo.list_notes(meeting_id)
    shots = await repo.list_assets(meeting_id)
    note_lines = [
        f"- {labels.get(n.kind, 'Not')}: {n.text}" + (f" ({n.author})" if n.author else "")
        for n in notes
    ]
    note_lines += [f"- Ekran görüntüsü {a.number}: {a.caption}" for a in shots if a.caption]
    notes_text = "\n".join(note_lines)

    if meeting.report and not resummarize:
        # Önceden üretilmiş (ör. "beni unut" sonrası anonimleştirilmiş) rapor varsa yeniden
        # özetlemeye gerek yok; sadece belgeleri yeniden bas.
        report = MeetingReport.model_validate(meeting.report)
    else:
        extra = {}
        if interim:
            extra["thinking"] = False  # ara rapor: hızlı ve ucuz; final raporu düşünerek yazılır
        if batch:
            previous = meeting.documents or {}
            started = previous.get("batch_started_at") or datetime.now(UTC).isoformat()
            marker = {"batch_pending": "true", "batch_started_at": started}
            if previous.get("batch"):
                marker["batch"] = previous["batch"]
            await repo.update_batch_marker(meeting_id, marker)

            async def remember(batch_id: str) -> None:
                await repo.update_batch_marker(
                    meeting_id, {"batch": batch_id, "batch_started_at": started}
                )

            extra = {
                "batch": True,
                "on_batch": remember,
                "resume_batch": (meeting.documents or {}).get("batch"),
            }
        try:
            with usage.meeting(meeting_id):
                report = await summarizer(
                    segments,
                    # Do not expose the entire company directory to an injected transcript.
                    org_context="\n".join(
                        f"{name}"
                        + (f" ({title})" if title else "")
                        + (f" <{email}>" if email else "")
                        for name, title, email in await _participants(meeting_id, segments)
                    ),
                    notes=notes_text,
                    lang=settings.report_lang,
                    **extra,
                )
        finally:
            if batch:
                # Silme aynı anda tamamlanmışsa satırı yeniden oluşturma.
                with contextlib.suppress(DataKeyError):
                    await repo.update_batch_marker(meeting_id, {})

    if meeting.title:
        # Panelden verilen toplantı adı raporun konusu olur.
        report = report.model_copy(update={"title": meeting.title})

    ctx = ReportContext(
        report=report,
        meeting_id=meeting_id,
        cipher=await meeting_cipher(meeting_id),
        started_at=meeting.started_at,
        ended_at=meeting.ended_at,
        platform=meeting.platform,
        company=settings.company_name,
        participants=await _participants(meeting_id, segments),
        notes=[(n.kind, n.text, n.author, n.at) for n in notes],
        segments=segments,
        include_transcript=settings.report_include_transcript,
        screenshots=[(a.number, a.path, a.caption, a.at_s) for a in shots],
    )
    out_dir = Path(settings.data_dir) / "reports" / meeting_id
    docx_path = await asyncio.to_thread(render_docx, ctx, out_dir / "toplantı-raporu.docx")
    # Transkript Word ekinde kalır; PDF kısa özet belgesidir (KATIP_REPORT_TRANSCRIPT_PDF).
    ctx.include_transcript = ctx.include_transcript and settings.report_transcript_pdf
    pdf_path = await render_pdf(ctx, out_dir / "toplantı-raporu.pdf")

    documents = {"docx": str(docx_path), "pdf": str(pdf_path)}
    await repo.set_report(meeting_id, report.model_dump(), documents)
    from katip.teams.service import report_ready

    await report_ready(meeting_id)
    log.info("rapor üretildi", meeting_id=meeting_id, **documents)
    return documents
