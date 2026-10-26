"""Rapor taslağı ve yalnız panel onayıyla, tek denemelik toplantı maili."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from katip.api.security import PanelAccess, absolute_url
from katip.config import get_settings, organizer_domains
from katip.identity import company_email, normalize_email
from katip.integrations.microsoft import MicrosoftGraph, configured
from katip.reports.summarize import MeetingReport
from katip.store import crypto
from katip.store.db import session_scope, write_scope
from katip.store.models import (
    AuditLog,
    Meeting,
    MeetingIdentity,
    MeetingMailDraft,
    MeetingParticipant,
    PanelAccount,
    Person,
)

PERMISSION_ERROR = "Exchange'de Katip için Mail.Send izni yok; kurulum adımlarına bakın"
UNCERTAIN = "Gönderim sonucu belirsiz; Gönderilmiş Öğeler'i kontrol edin. Yeniden gönderilmez."
REQUEST_LIMIT = 3_000_000


def internal_domains() -> list[str]:
    s = get_settings()
    if s.mail_internal_domains:
        return organizer_domains(s.mail_internal_domains.split(","))
    try:
        return organizer_domains([normalize_email(s.m365_mailbox).rsplit("@", 1)[1]])
    except ValueError:
        return []


def address(value: str) -> str:
    value = normalize_email(value)
    organizer_domains([value.rsplit("@", 1)[1]])
    if re.search(r"[\x00-\x20\x7f,;:\"\\()]", value):
        raise ValueError("Geçerli bir e-posta adresi girin")
    return value


def disabled_reason() -> str:
    if not get_settings().mail_enabled:
        return "Mail gönderimi kapalı (KATIP_MAIL_ENABLED)"
    if not configured():
        return "Microsoft 365 bağlantısı yapılandırılmadı"
    return ""


async def health() -> tuple[bool | None, str]:
    if disabled_reason():
        return None, "kapalı · " + disabled_reason()
    async with session_scope() as session:
        row = await session.scalar(
            select(MeetingMailDraft)
            .where(MeetingMailDraft.attempted_at.is_not(None))
            .order_by(MeetingMailDraft.attempted_at.desc())
            .limit(1)
        )
        if row and row.permission_error:
            return False, "izin hatası son denemede"
    return True, "hazır"


async def _meeting(session, mid: str) -> Meeting:
    meeting = await session.scalar(select(Meeting).where(Meeting.id == mid).with_for_update())
    if meeting is None:
        raise HTTPException(404, "Toplantı bulunamadı")
    return meeting


async def _erasure_check(session) -> None:
    if await session.scalar(select(Person.id).where(Person.erasure_pending.is_(True)).limit(1)):
        raise HTTPException(409, "Kişisel veri silme işlemi tamamlanınca tekrar deneyin")


async def _candidates(session, meeting: Meeting) -> list[dict]:
    found = {}
    identities = await session.scalars(
        select(MeetingIdentity).where(MeetingIdentity.meeting_id == meeting.id)
    )
    participants = await session.scalars(
        select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id)
    )
    entries = [(i.name, i.email, i.source) for i in identities]
    entries += [(p.name, p.email, p.identity_source) for p in participants]
    owner = (
        await session.get(PanelAccount, meeting.owner_account_id)
        if meeting.owner_account_id
        else None
    )
    if owner:
        entries.append((owner.email, owner.email, "panel"))
    for name, raw, source in entries:
        if not raw or source not in {"panel", "calendar", "meeting"}:
            continue
        try:
            email = address(raw)
        except ValueError:
            continue
        found[email] = {
            "email": email,
            "name": name,
            "internal": company_email(email, internal_domains()),
        }
    return list(found.values())


async def _draft(session, meeting: Meeting) -> MeetingMailDraft:
    row = await session.scalar(
        select(MeetingMailDraft).where(MeetingMailDraft.meeting_id == meeting.id).with_for_update()
    )
    if row:
        return row
    await _erasure_check(session)
    if not meeting.report:
        raise HTTPException(409, "Toplantı raporu henüz hazır değil")
    report = MeetingReport.model_validate(meeting.report)
    candidates = await _candidates(session, meeting)
    started = meeting.started_at
    started = started.replace(tzinfo=UTC) if started.tzinfo is None else started
    date = started.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%d.%m.%Y")
    title = re.sub(r"[\x00-\x1f\x7f]", " ", meeting.title or report.title)
    row = MeetingMailDraft(
        id=uuid.uuid4().hex,
        meeting_id=meeting.id,
        payload={
            "subject": f"Toplantı notu: {title} — {date}",
            "report": report.model_dump(),
            "candidates": candidates,
            "recipients": [c["email"] for c in candidates if c["internal"]][:50],
            "intro": "",
            "approver": None,
            "panel_url": absolute_url(f"/panel/{quote(meeting.id, safe='')}"),
        },
    )
    session.add(row)
    await session.flush()
    return row


def body(payload: dict, mid: str) -> tuple[str, str]:
    report = MeetingReport.model_validate(payload["report"])
    sections = []
    if payload["intro"]:
        sections.append(("Giriş notu", [payload["intro"]]))
    sections.append(("Özet", [report.summary]))
    sections.append(
        (
            "Kararlar",
            [d.text + (f" — {d.owner}" if d.owner else "") for d in report.decisions]
            or ["Kayıt yok."],
        )
    )
    sections.append(
        (
            "Aksiyonlar",
            [
                f"{a.task} — Sorumlu: {a.owner or 'Belirtilmedi'}; "
                f"Termin: {a.due or 'Belirtilmedi'}"
                for a in report.action_items
            ]
            or ["Kayıt yok."],
        )
    )
    footer = (
        f"Katip tarafından hazırlandı; {payload['approver']} tarafından onaylandı."
        if payload["approver"]
        else "Katip tarafından hazırlandı; toplantı sahibi onayı bekleniyor."
    )
    link = payload.get("panel_url") or absolute_url(f"/panel/{quote(mid, safe='')}")
    text = "\n\n".join(title + "\n" + "\n".join(lines) for title, lines in sections)
    text += f"\n\n{footer}\nPanel raporu: {link}"
    markup = "".join(
        f"<h2>{html.escape(title)}</h2>"
        + "".join("<p>" + html.escape(line).replace("\n", "<br>") + "</p>" for line in lines)
        for title, lines in sections
    )
    markup += f"<p>{html.escape(footer)}</p><p>Panel raporu: {html.escape(link)}</p>"
    return text, markup


def _view(row: MeetingMailDraft, can_manage: bool) -> dict:
    result = {
        "status": row.status,
        "sent_at": row.sent_at,
        "error": row.error,
        "can_manage": can_manage,
    }
    if can_manage:
        text, markup = body(row.payload, row.meeting_id)
        result.update(
            draft_id=row.id,
            version=row.version,
            subject=row.payload["subject"],
            body_text=text,
            body_html=markup,
            candidates=[
                {**c, "internal": company_email(c["email"], internal_domains())}
                for c in row.payload["candidates"]
            ],
            recipients=row.payload["recipients"],
            intro=row.payload["intro"],
            disabled_reason=disabled_reason(),
            request_id=row.request_id,
            client_request_id=row.client_request_id,
        )
    return result


async def get_draft(mid: str, access: PanelAccess) -> dict:
    can_manage = await access.allows(mid, "manage")
    async with write_scope() as session:
        row = await _draft(session, await _meeting(session, mid))
        return _view(row, can_manage)


def _editable(row, version, draft_id=None):
    if row.status not in {"taslak", "hata"}:
        raise HTTPException(409, "Gönderilen veya gönderilmekte olan taslak değiştirilemez")
    if row.version != version or (draft_id is not None and row.id != draft_id):
        raise HTTPException(409, "Taslak değişti; yeniden açın ve onaylayın")


async def edit(
    mid: str, version: int, recipients: list[str], intro: str, *, draft_id: str | None = None
) -> dict:
    async with write_scope() as session:
        row = await _draft(session, await _meeting(session, mid))
        _editable(row, version, draft_id)
        await _erasure_check(session)
        known = {c["email"] for c in row.payload["candidates"]}
        try:
            recipients = list(dict.fromkeys(address(r) for r in recipients))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if len(recipients) > 50:
            raise HTTPException(422, "En fazla 50 alıcı seçilebilir")
        candidates = list(row.payload["candidates"])
        for email in recipients:
            if email not in known:
                if not company_email(email, internal_domains()):
                    raise HTTPException(422, "Ek adres yalnız iç alan adlarından olabilir")
                candidates.append({"email": email, "name": email, "internal": True})
        row.payload = {
            **row.payload,
            "recipients": recipients,
            "intro": intro,
            "candidates": candidates,
            "approver": None,
        }
        row.version += 1
        row.status, row.error = "taslak", None
        await session.flush()
        return _view(row, True)


async def mime_message(row: MeetingMailDraft, meeting: Meeting) -> bytes:
    text, markup = body(row.payload, meeting.id)
    msg = EmailMessage(policy=SMTP)
    msg["Subject"] = row.payload["subject"]
    msg["From"] = address(get_settings().m365_mailbox)
    msg["To"] = ", ".join(row.payload["recipients"])
    msg.set_content(text)
    msg.add_alternative(markup, subtype="html")
    path = (meeting.documents or {}).get("pdf")
    if path and await asyncio.to_thread(Path(path).is_file):
        size = await asyncio.to_thread(lambda: Path(path).stat().st_size)
        if size < REQUEST_LIMIT:
            pdf = await crypto.read_file(meeting.id, path)
            if len(pdf) < REQUEST_LIMIT:
                msg.add_attachment(
                    pdf, maintype="application", subtype="pdf", filename="toplantı-raporu.pdf"
                )
                if len(base64.b64encode(msg.as_bytes())) >= REQUEST_LIMIT:
                    del msg.get_payload()[-1]
    raw = msg.as_bytes()
    if len(base64.b64encode(raw)) >= REQUEST_LIMIT:
        raise HTTPException(422, "Mail içeriği gönderim sınırını aşıyor")
    return raw


async def send(mid: str, version: int, access: PanelAccess, *, draft_id: str | None = None) -> dict:
    reason = disabled_reason()
    if reason:
        raise HTTPException(409, reason)
    if urlsplit(absolute_url("/panel/")).scheme not in {"https", "http"}:
        raise HTTPException(409, "Panel bağlantısı için KATIP_PUBLIC_BASE_URL tanımlayın")
    async with write_scope() as session:
        meeting = await _meeting(session, mid)
        row = await _draft(session, meeting)
        _editable(row, version, draft_id)
        await _erasure_check(session)
        if not row.payload["recipients"]:
            raise HTTPException(422, "En az bir alıcı seçin")
        account = await session.get(PanelAccount, access.account_id) if access.account_id else None
        row.payload = {
            **row.payload,
            "approver": account.email if account else "Yönetici",
            "panel_url": absolute_url(f"/panel/{quote(mid, safe='')}"),
        }
        raw = await mime_message(row, meeting)
        row.status, row.error = "gönderiliyor", None
        row.approved_account_id = access.account_id
        row.attempted_at = datetime.now(UTC)
        row.client_request_id = uuid.uuid4().hex
        row.permission_error = False
        await session.flush()
        draft_id, request_id = row.id, row.client_request_id
        session.add(
            AuditLog(
                actor=f"panel:{access.account_id}" if access.account_id else "admin",
                action="mail.approved",
                detail={
                    "meeting_id": mid,
                    "version": version,
                    "recipient_count": len(row.payload["recipients"]),
                    "recipient_hashes": [
                        hashlib.sha256(r.encode()).hexdigest() for r in row.payload["recipients"]
                    ],
                },
            )
        )
    # Onay kalıcıdır; süreç kapanır veya ağ sonucu belirsizse otomatik tekrar yapılmaz.
    response = None
    error, retryable, permission = UNCERTAIN, False, False
    try:
        async with (
            asyncio.timeout(150),
            httpx.AsyncClient(timeout=20, follow_redirects=False) as client,
        ):
            response = await MicrosoftGraph(client).send_mail(raw, request_id)
    except httpx.HTTPStatusError as exc:
        response = exc.response
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Bağlantı hiç kurulmadı; istek Microsoft'a ulaşmadığı için yeniden onaylanabilir.
        error, retryable = "Microsoft'a bağlanılamadı; daha sonra yeniden onaylayın", True
    except (httpx.HTTPError, TimeoutError):
        pass
    if response is not None:
        status = response.status_code
        if status in {401, 403}:
            error, retryable, permission = PERMISSION_ERROR, True, True
        elif status == 429:
            error, retryable = "Microsoft gönderim sınırı dolu; daha sonra yeniden onaylayın", True
        elif 400 <= status < 500:
            error, retryable = "Microsoft mail isteğini reddetti; taslağı kontrol edin", True
    async with write_scope() as session:
        await _meeting(session, mid)
        row = await session.get(MeetingMailDraft, draft_id)
        if row is None or row.client_request_id != request_id:
            raise HTTPException(409, "Gönderim kaydı bulunamadı")
        row.request_id = (
            response.headers.get("request-id", "")[:200] if response is not None else None
        )
        row.permission_error = permission
        if response is not None and response.status_code == 202:
            row.status, row.sent_at, row.error = "gönderildi", datetime.now(UTC), None
        else:
            row.status, row.error = "hata" if retryable else "gönderiliyor", error
        session.add(
            AuditLog(
                actor=f"panel:{access.account_id}" if access.account_id else "admin",
                action="mail.sent" if row.status == "gönderildi" else "mail.failed",
                detail={
                    "meeting_id": mid,
                    "version": version,
                    "request_id": row.request_id,
                    "client_request_id": request_id,
                },
            )
        )
        await session.flush()
        result = _view(row, True)
    if result["status"] != "gönderildi":
        raise HTTPException(502, error)
    return result
