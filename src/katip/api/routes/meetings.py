"""Toplantı oturumu REST uçları."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from katip.api.security import (
    PanelAccess,
    require_meeting_admin,
    require_meeting_manage,
    require_meeting_read,
    require_operator,
    require_panel,
    require_viewer,
    view_link_params,
)
from katip.async_utils import LoopLocal
from katip.bot.platforms.base import platform_for
from katip.bot.supervisor import LimitExceeded, supervisor
from katip.config import get_settings
from katip.events import Segment, SessionState
from katip.identity import ParticipantIdentity
from katip.pipeline.session import registry
from katip.reports.service import generate_report
from katip.store import repo
from katip.workers import jobs as worker_jobs

router = APIRouter(prefix="/api/meetings", tags=["meetings"])
_join_lock = LoopLocal(asyncio.Lock)


class MailSelection(BaseModel):
    draft_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: int = Field(ge=1)
    recipients: list[str] = Field(max_length=50)
    intro: str = Field(default="", max_length=1000)


class MailApproval(BaseModel):
    draft_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: int = Field(ge=1)


@router.get("/{session_id}/mail")
async def mail_draft(
    session_id: str, access: Annotated[PanelAccess, Depends(require_meeting_read)]
) -> dict:
    from katip.integrations import mail

    return await mail.get_draft(session_id, access)


@router.put("/{session_id}/mail")
async def mail_selection(
    session_id: str,
    req: MailSelection,
    access: Annotated[PanelAccess, Depends(require_meeting_manage)],
) -> dict:
    from katip.integrations import mail

    return await mail.edit(
        session_id, req.version, req.recipients, req.intro, draft_id=req.draft_id
    )


@router.post("/{session_id}/mail/send")
async def mail_send(
    session_id: str,
    req: MailApproval,
    access: Annotated[PanelAccess, Depends(require_meeting_manage)],
) -> dict:
    from katip.integrations import mail

    return await mail.send(session_id, req.version, access, draft_id=req.draft_id)


class JoinRequest(BaseModel):
    url: str = Field(max_length=4096, description="Meet / Teams / Zoom toplantı linki")
    source_lang: str | None = None
    target_lang: str | None = None
    title: str | None = Field(None, max_length=200, description="Toplantı adı (rapor konusu)")
    participants: list[ParticipantIdentity] = Field(default_factory=list, max_length=200)
    preparation_id: str | None = Field(None, pattern=r"^[a-f0-9]{12}$")
    mode: Literal["tam", "kayit"] | None = Field(
        None, description='"tam" (agent + ses + çeviri) ya da "kayit" (gün boyu sessiz kayıt)'
    )


class JoinResponse(BaseModel):
    session_id: str
    platform: str
    panel_url: str
    mode: str = "tam"


@router.post("", response_model=JoinResponse)
async def join_meeting(
    req: JoinRequest, access: Annotated[PanelAccess, Depends(require_operator)]
) -> JoinResponse:
    """Botu toplantıya sokar. Yönetici token'ı ister: aksi hâlde açık bir sunucuda herkes
    bot başlatıp STT/LLM kredisi harcatabilir."""
    if req.preparation_id:
        await access.check(req.preparation_id, "manage")
    return await start_meeting(req, owner_account_id=access.account_id)


async def start_meeting(
    req: JoinRequest,
    *,
    owner_account_id: int | None = None,
    before_launch: Callable[[str], Awaitable[None]] | None = None,
    reject_duplicate: bool = False,
    conversation_id: str | None = None,
) -> JoinResponse:
    """Shared authorized API/scheduler path; serialize capacity and budget checks."""
    async with _join_lock.get():
        if reject_duplicate:
            from katip.teams.store import matching_meetings

            if await matching_meetings(req.url, conversation_id=conversation_id, live_only=True):
                raise HTTPException(409, "Katip bu toplantıda zaten canlı; yeniden katılamaz.")
        return await _start_meeting(
            req, owner_account_id=owner_account_id, before_launch=before_launch
        )


async def _start_meeting(
    req: JoinRequest,
    *,
    owner_account_id: int | None = None,
    before_launch: Callable[[str], Awaitable[None]] | None = None,
) -> JoinResponse:
    try:
        platform_cls = platform_for(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = get_settings()
    try:
        supervisor.check_capacity()
    except LimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    used = await repo.meeting_minutes_today()
    if used >= settings.daily_meeting_minutes:
        raise HTTPException(
            status_code=429,
            detail=f"günlük bot süresi doldu ({used:.0f}/{settings.daily_meeting_minutes} dk)",
        )
    if await worker_jobs.count_open() >= settings.max_concurrent_bots:
        raise HTTPException(429, "Worker kapasitesi dolu veya kapanış doğrulaması bekleniyor")

    session_id = req.preparation_id or uuid.uuid4().hex[:12]
    if req.preparation_id:
        prepared = await repo.get_meeting(session_id)
        if prepared is None or prepared.state != "prepared":
            raise HTTPException(409, "Hazırlık bulunamadı veya zaten kullanıldı")
    try:
        session = registry.create(
            session_id,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            mode=req.mode or settings.default_mode,
        )
    except RuntimeError as exc:  # ör. SONIOX_API_KEY tanımlı değil
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await repo.upsert_meeting(
        session_id,
        platform=platform_cls.platform.value,
        url=req.url,
        title=(req.title or "").strip() or None,
        source_lang=session.source_lang,
        target_lang=session.target_lang,
        state="joining",
        owner_account_id=owner_account_id,
    )
    if req.participants:
        await session.add_identities(req.participants, "panel")
    if platform_cls.platform.value == "teams":
        from katip.integrations.microsoft import sync_identities

        session._spawn(sync_identities(session_id))
    try:
        if before_launch:
            await before_launch(session_id)
        await worker_jobs.enqueue(
            session_id,
            {
                "url": req.url,
                "source_lang": session.source_lang,
                "target_lang": session.target_lang,
                "mode": session.mode,
                "max_minutes": session.max_minutes,
                "participants": [p.model_dump() for p in req.participants],
            },
            session.max_minutes,
        )
        await supervisor.start(session_id, req.url, max_minutes=session.max_minutes)
    except Exception as exc:
        if session_id in supervisor._containers or session_id in supervisor._procs:
            await worker_jobs.request_cancel(session_id, "start_uncertain")
        else:
            await worker_jobs.finish_local(session_id, failed=True)
        await registry.drop(session_id)
        await repo.finish_meeting(session_id, "error")
        raise HTTPException(503, "Toplantı worker'ı başlatılamadı") from exc
    await repo.audit(
        f"panel:{owner_account_id}" if owner_account_id else "system",
        "meeting.joined",
        meeting_id=session_id,
        platform=platform_cls.platform.value,
    )
    return JoinResponse(
        session_id=session_id,
        platform=platform_cls.platform.value,
        panel_url=f"/panel/{session_id}?{view_link_params(session_id)}",
        mode=session.mode,
    )


class ShareLinks(BaseModel):
    panel_url: str
    report_pdf: str
    report_docx: str
    expires_days: float


async def require_grant_manager(
    session_id: str, access: Annotated[PanelAccess, Depends(require_meeting_manage)]
) -> PanelAccess:
    meeting = await repo.get_meeting(session_id)
    if meeting is None:
        raise HTTPException(404, "Toplantı bulunamadı")
    if access.role != "admin" and meeting.owner_account_id != access.account_id:
        raise HTTPException(403, "İzinleri yalnızca yönetici veya toplantı sahibi değiştirebilir")
    return access


class GrantRequest(BaseModel):
    account_id: int | None = Field(None, gt=0)
    email: str | None = Field(None, max_length=320)
    permission: Literal["read", "manage"]


@router.get("/{session_id}/grants", dependencies=[Depends(require_grant_manager)])
async def grants(session_id: str) -> dict:
    meeting = await repo.get_meeting(session_id)
    owner = (
        await repo.get_panel_account(meeting.owner_account_id) if meeting.owner_account_id else None
    )
    rows = []
    for grant in await repo.list_meeting_grants(session_id):
        account = await repo.get_panel_account(grant.account_id)
        rows.append(
            {
                "account_id": grant.account_id,
                "email": account.email if account else None,
                "permission": grant.permission,
            }
        )
    return {
        "owner_account_id": meeting.owner_account_id,
        "owner_email": owner.email if owner else None,
        "grants": rows,
    }


@router.post("/{session_id}/grants")
async def add_grant(
    session_id: str,
    req: GrantRequest,
    access: Annotated[PanelAccess, Depends(require_grant_manager)],
) -> dict:
    from katip.identity import normalize_email

    account = None
    if req.email:
        try:
            account = await repo.find_panel_account(normalize_email(req.email))
        except ValueError:
            pass
    elif access.role == "admin" and req.account_id:
        account = await repo.get_panel_account(req.account_id)
    if account is None or not account.enabled:
        raise HTTPException(404, "İzin verilecek hesap bulunamadı")
    await repo.set_meeting_grant(session_id, account.id, req.permission)
    await repo.audit(
        f"panel:{access.account_id}" if access.account_id else "admin",
        "meeting_grant_added",
        meeting_id=session_id,
        account_id=account.id,
        permission=req.permission,
    )
    return {"account_id": account.id, "permission": req.permission}


@router.delete("/{session_id}/grants/{account_id}")
async def remove_grant(
    session_id: str, account_id: int, access: Annotated[PanelAccess, Depends(require_grant_manager)]
) -> dict:
    if not await repo.remove_meeting_grant(session_id, account_id):
        raise HTTPException(404, "İzin bulunamadı")
    await repo.audit(
        f"panel:{access.account_id}" if access.account_id else "admin",
        "meeting_grant_removed",
        meeting_id=session_id,
        account_id=account_id,
    )
    return {"deleted": True}


@router.post(
    "/{session_id}/share", response_model=ShareLinks, dependencies=[Depends(require_meeting_manage)]
)
async def share_links(session_id: str, days: float | None = None) -> ShareLinks:
    """Toplantıya özel, süreli görüntüleme linkleri (panel ve rapor) üretir."""
    if await repo.get_meeting(session_id) is None and registry.get(session_id) is None:
        raise HTTPException(status_code=404, detail="oturum bulunamadı")
    days = days if days is not None else get_settings().share_link_days
    q = view_link_params(session_id, days=days)
    base = f"/api/meetings/{session_id}"
    return ShareLinks(
        panel_url=f"/panel/{session_id}?{q}",
        report_pdf=f"{base}/report.pdf?{q}",
        report_docx=f"{base}/report.docx?{q}",
        expires_days=days,
    )


class SessionInfo(BaseModel):
    session_id: str
    state: SessionState
    segment_count: int
    mode: str
    participants: list[str]
    can_manage: bool


@router.get("", response_model=list[SessionInfo])
async def list_sessions(
    access: Annotated[PanelAccess, Depends(require_panel)],
) -> list[SessionInfo]:
    allowed = await access.meeting_ids()
    managed = await access.meeting_ids("manage")
    return [
        SessionInfo(
            session_id=s.session_id,
            state=s.state,
            segment_count=len(s.final_segments()),
            mode=s.mode,
            participants=list(s._roster),
            can_manage=managed is None or s.session_id in managed,
        )
        for s in registry.all()
        if allowed is None or s.session_id in allowed
    ]


@router.get(
    "/{session_id}/transcript",
    response_model=list[Segment],
    dependencies=[Depends(require_viewer)],
)
async def transcript(session_id: str) -> list[Segment]:
    session = registry.get(session_id)
    if session is not None:
        return session.final_segments()
    meeting = await repo.get_meeting(session_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="oturum bulunamadı")
    return [
        Segment(
            id=s.id,
            session_id=session_id,
            speaker=s.speaker_name,
            speaker_label=s.speaker_label,
            speaker_email=s.speaker_email,
            start_s=s.start_s,
            end_s=s.end_s,
            text=s.text,
            lang=s.lang,
            is_final=True,
            translation=s.translation,
        )
        for s in meeting.segments
    ]


@router.get("/{session_id}/report", dependencies=[Depends(require_viewer)])
async def report(session_id: str) -> dict:
    meeting = await repo.get_meeting(session_id)
    if meeting is None or not meeting.report:
        raise HTTPException(status_code=404, detail="rapor yok")
    return {"report": meeting.report, "documents": sorted(meeting.documents or {})}


@router.post("/{session_id}/report", dependencies=[Depends(require_meeting_manage)])
async def regenerate_report(session_id: str) -> dict:
    """Raporu (yeniden) üretir. Özet varsa korunur, belgeler yeniden basılır."""
    try:
        documents = await generate_report(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not documents:
        raise HTTPException(status_code=422, detail="rapor için yeterli konuşma yok")
    return {"documents": sorted(documents)}


_MEDIA = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.get("/{session_id}/report.{fmt}", dependencies=[Depends(require_viewer)])
async def report_file(session_id: str, fmt: str) -> Response:
    if fmt not in _MEDIA:
        raise HTTPException(status_code=404, detail="desteklenmeyen biçim")
    meeting = await repo.get_meeting(session_id)
    path = (meeting.documents or {}).get(fmt) if meeting else None
    if not path or not await asyncio.to_thread(os.path.exists, path):
        raise HTTPException(status_code=404, detail="belge yok; POST /report ile üretin")
    from urllib.parse import quote

    from katip.store.crypto import read_file

    filename = quote(f"toplantı-{session_id}.{fmt}")
    return Response(
        await read_file(session_id, path),
        media_type=_MEDIA[fmt],
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename}"},
    )


@router.delete("/{session_id}", dependencies=[Depends(require_meeting_manage)])
async def leave_meeting(session_id: str) -> dict:
    status = await worker_jobs.request_cancel(session_id)
    stopped = await supervisor.stop(session_id)
    await registry.drop(session_id)
    if status is not None:
        current = await worker_jobs.get(session_id)
        status = current.status if current else status
    return {
        "stopped": stopped,
        "worker_status": status,
        "stop_confirmed": status in ("cancelled", "completed", "failed"),
    }


class ModeRequest(BaseModel):
    mode: str = Field(description='"chat" (tamamen yazılı) ya da "voice" (tamamen sesli)')


class PresentationRequest(BaseModel):
    action: Literal["start", "next", "prev", "goto", "stop", "auto", "pause", "resume"]
    page: int | None = Field(None, ge=1, le=60, strict=True)


@router.get("/{session_id}/presentation", dependencies=[Depends(require_meeting_read)])
async def presentation_status(session_id: str) -> dict:
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(404, "Aktif oturum bulunamadı")
    return session.presentation_state


@router.post("/{session_id}/presentation", dependencies=[Depends(require_meeting_manage)])
async def presentation_control(session_id: str, req: PresentationRequest) -> dict:
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(404, "Aktif oturum bulunamadı")
    if req.action == "goto" and req.page is None:
        raise HTTPException(422, "Slayt numarası gerekli")
    result = await session.presentation_action(req.action, req.page)
    await repo.audit(
        "panel", "presentation_control", meeting_id=session_id, presentation_action=req.action
    )
    return result


@router.delete("/{session_id}/content", dependencies=[Depends(require_meeting_admin)])
async def delete_content(session_id: str) -> dict:
    """Bitmiş toplantıyı, anahtar zarfını ve dosyalarını kalıcı siler."""
    from sqlalchemy import select

    from katip.store.db import session_scope
    from katip.store.models import WorkerJob

    async with session_scope() as session:
        status = await session.scalar(
            select(WorkerJob.status).where(WorkerJob.meeting_id == session_id)
        )
    if registry.get(session_id) is not None or status in worker_jobs.OPEN:
        raise HTTPException(409, "Önce toplantıyı bitirin ve worker'ın durmasını bekleyin")
    from katip.reports.service import ReportPendingError

    try:
        if not await repo.delete_meeting(session_id, cancel_pending=True):
            raise HTTPException(404, "Toplantı bulunamadı")
    except ReportPendingError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": True}


@router.post("/{session_id}/mode", dependencies=[Depends(require_meeting_manage)])
async def set_mode(session_id: str, req: ModeRequest) -> dict[str, str]:
    """Toplantı sırasında cevap modunu değiştirir (panelden)."""
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="oturum bulunamadı")
    try:
        session.set_reply_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"mode": session.reply_mode}


@router.get("/{session_id}/screenshots/{number}", dependencies=[Depends(require_viewer)])
async def screenshot(session_id: str, number: int) -> Response:
    assets = await repo.list_assets(session_id)
    asset = next((a for a in assets if a.number == number), None)
    if asset is None or not await asyncio.to_thread(os.path.exists, asset.path):
        raise HTTPException(status_code=404, detail="ekran görüntüsü bulunamadı")
    from katip.store.crypto import read_file

    return Response(await read_file(session_id, asset.path), media_type="image/jpeg")


def _debug_path(session_id: str, ext: str) -> str:
    # Oturum kimliği dosya yoluna girdiği için yalnızca güvenli karakterlere izin ver.
    if not session_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="geçersiz oturum kimliği")
    return os.path.join(get_settings().data_dir, "debug", f"{session_id}.{ext}")


@router.get("/{session_id}/debug", dependencies=[Depends(require_meeting_admin)])
async def debug_info(session_id: str) -> FileResponse:
    """Katılma başarısız olduğunda botun kaydettiği sayfa teşhisi (düğmeler, alanlar)."""
    path = _debug_path(session_id, "json")
    if not await asyncio.to_thread(os.path.exists, path):
        raise HTTPException(status_code=404, detail="teşhis kaydı yok")
    return FileResponse(path, media_type="application/json")


@router.get("/{session_id}/debug.png", dependencies=[Depends(require_meeting_admin)])
async def debug_screenshot(session_id: str) -> FileResponse:
    path = _debug_path(session_id, "png")
    if not await asyncio.to_thread(os.path.exists, path):
        raise HTTPException(status_code=404, detail="teşhis görüntüsü yok")
    return FileResponse(path, media_type="image/png")


@router.post("/{session_id}/diagnose", dependencies=[Depends(require_meeting_admin)])
async def request_diagnose(session_id: str) -> dict[str, bool]:
    """Toplantı sürerken botun sayfa teşhisini (düğmeler, alanlar, ekran görüntüsü) iste.

    Sonuç birkaç saniye içinde GET /{id}/debug ve /{id}/debug.png'den okunur.
    """
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="oturum bulunamadı")
    session.outbox.put_nowait({"kind": "diagnose"})
    return {"requested": True}


@router.get("/{session_id}/debug.html", dependencies=[Depends(require_meeting_admin)])
async def debug_html(session_id: str) -> FileResponse:
    """Teşhis anındaki sayfa HTML'i (tüm çerçeveler) — platform seçicilerini yazmak için."""
    path = _debug_path(session_id, "html")
    if not await asyncio.to_thread(os.path.exists, path):
        raise HTTPException(status_code=404, detail="teşhis HTML'i yok")
    # Yakalanan sayfanın script'leri Katip'in kendi adresinde çalışmasın: düz metin + indirme
    # + her şeyi yasaklayan CSP.
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=f"{session_id}.html.txt",
        headers={"Content-Security-Policy": "sandbox; default-src 'none'"},
    )
