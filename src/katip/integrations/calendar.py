"""Graph takvimi için panel politikası, onay kuyruğu ve zamanında katılım."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select, update

from katip.bot.platforms.base import platform_for
from katip.config import get_settings
from katip.identity import ParticipantIdentity, company_email, normalize_email
from katip.integrations.microsoft import MicrosoftGraph, configured, meeting_url_key
from katip.logging import get_logger
from katip.store import repo
from katip.store.db import session_scope, write_scope
from katip.store.models import AuditLog, CalendarJoin, CalendarJoinGuard, PanelAccount

log = get_logger(__name__)
REAPPROVAL_REASON = "Davet değişti; yeniden onay gerekiyor"
CLOSED_REASON = "Takvim katılımı kapalı"
EXTERNAL_PENDING_LIMIT = 5
MANUAL_REFRESH_INTERVAL_S = 15


def event_url(event: dict) -> str:
    """Yapılandırılmış bağlantıları okur; davet metni talimat değildir."""
    direct = (event.get("onlineMeeting") or {}).get("joinUrl") or event.get("onlineMeetingUrl")
    if direct:
        platform_for(direct)
        return direct
    # Harici Meet/Zoom davetlerinde bağlantı konum alanında bulunabilir.
    location = (event.get("location") or {}).get("displayName", "")
    links = set(re.findall(r"https://[^\s<>\"']+", location))
    supported = []
    for link in links:
        with contextlib.suppress(ValueError):
            platform_for(link)
            supported.append(link)
    if len(supported) != 1:
        raise ValueError("Toplantı bağlantısı eksik veya belirsiz")
    return supported[0]


def event_start(event: dict) -> datetime:
    raw = (event.get("start") or {}).get("dateTime", "")
    start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # Graph isteği UTC ister; farklı saat dilimi tahmin edilmez.
    zone = (event.get("start") or {}).get("timeZone", "UTC")
    if start.tzinfo is None and zone not in ("UTC", "Etc/UTC"):
        raise ValueError("Beklenmeyen takvim saat dilimi")
    return start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)


def eligible(event: dict) -> bool:
    """Yalnız yapılandırılmış alanlardan teknik uygunluğu denetler."""
    s = get_settings()
    if not event.get("id") or event.get("isCancelled") or event.get("isAllDay"):
        return False
    if (event.get("responseStatus") or {}).get("response") == "declined":
        return False
    address = (event.get("organizer") or {}).get("emailAddress") or {}
    try:
        organizer = normalize_email(address.get("address", ""))
        mailbox = normalize_email(s.m365_mailbox)
        invited = organizer == mailbox or any(
            normalize_email((a.get("emailAddress") or {}).get("address", "")) == mailbox
            for a in event.get("attendees", [])
        )
        event_url(event)
        event_start(event)
        if event.get("end"):
            event_start(dict(event, start=event["end"]))
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(invited)


def organizer_info(event: dict) -> dict:
    address = (event.get("organizer") or {}).get("emailAddress") or {}
    raw = address.get("address", "")
    try:
        email = normalize_email(raw)
    except ValueError:
        return {
            "organizer_email": raw,
            "organizer_name": str(address.get("name") or "")[:200],
            "internal": False,
            "trusted": False,
            "organizer_kind": "dış",
        }
    s = get_settings()
    allowed_emails = set()
    for allowed in s.calendar_organizer_emails:
        try:
            allowed_emails.add(normalize_email(allowed))
        except ValueError:
            continue
    trusted = company_email(email, s.calendar_organizer_domains) or email in allowed_emails
    internal = company_email(email, s.panel_email_domains)
    return {
        "organizer_email": email,
        "organizer_name": str(address.get("name") or "")[:200],
        "internal": internal,
        "trusted": trusted,
        "organizer_kind": "güvenilir" if trusted else "iç" if internal else "dış",
    }


def decision(event: dict) -> str:
    """Politika tablosu: kapalı, otomatik veya onay."""
    mode = get_settings().calendar_join_policy
    if mode == "kapalı" or not eligible(event):
        return "kapalı"
    if mode == "hepsi" or (mode == "güvenilir" and organizer_info(event)["trusted"]):
        return "otomatik"
    return "onay"


def summary(event: dict, reason: str = "") -> dict:
    end = dict(event, start=event.get("end") or event["start"])
    url = event_url(event)
    host = urlsplit(url).hostname
    return {
        **organizer_info(event),
        "title": str(event.get("subject") or "")[:200],
        "starts_at": event_start(event).isoformat(),
        "ends_at": event_start(end).isoformat(),
        "platform": platform_for(url).platform.value,
        "platform_host": host,
        "masked_url": f"https://{host}/…",
        "join_url_hash": hashlib.sha256(json.dumps(meeting_url_key(url)).encode()).hexdigest(),
        "attendee_count": len(event.get("attendees", [])),
        "reason": reason
        or (
            CLOSED_REASON
            if get_settings().calendar_join_policy == "kapalı"
            else "Politika her davet için onay istiyor"
            if get_settings().calendar_join_policy == "onay"
            else "Organizatör güven listesinde değil"
        ),
    }


def approval_snapshot(details: dict) -> dict:
    return {
        key: details.get(key)
        for key in ("organizer_email", "join_url_hash", "starts_at", "ends_at")
    }


def _priority(details: dict) -> bool:
    info = organizer_info({"organizer": {"emailAddress": {"address": details["organizer_email"]}}})
    return info["trusted"] or info["internal"]


def _insert(session):
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


@contextlib.asynccontextmanager
async def guarded():
    """Sayaç kontrolü ve karar yazımı aynı veritabanı kilidini paylaşır."""
    async with write_scope() as session:
        await session.execute(
            _insert(session)(CalendarJoinGuard).values(id=1).on_conflict_do_nothing()
        )
        await session.execute(
            update(CalendarJoinGuard).where(CalendarJoinGuard.id == 1).values(id=1)
        )
        yield session


async def notification_hook(event_key: str, status: str) -> None:
    """İleride Graph Mail/Teams bildirimi buraya bağlanabilir; şu an gönderim yapmaz."""


async def _pending(session, event: dict, reason: str = "", row: CalendarJoin | None = None) -> bool:
    details = summary(event, reason)
    pending = list(
        await session.scalars(
            select(CalendarJoin)
            .where(CalendarJoin.status == "pending")
            .order_by(CalendarJoin.created_at, CalendarJoin.id)
        )
    )
    priority = _priority(details)
    limited = False
    if not priority:
        limited = (
            sum(
                (r.summary or {}).get("organizer_email") == details["organizer_email"]
                for r in pending
            )
            >= EXTERNAL_PENDING_LIMIT
        )
    if not limited and len(pending) >= get_settings().calendar_max_pending:
        external = next((r for r in pending if r.summary and not _priority(r.summary)), None)
        if priority and external is not None:
            # Onay iptalinin izi silinmez; politika değişince eski davet otomatiğe kaçamaz.
            external.status = "evicted"
            await session.flush()
            (await session.get(CalendarJoinGuard, 1)).queue_limited = True
        else:
            limited = True
    if limited:
        (await session.get(CalendarJoinGuard, 1)).queue_limited = True
        return False
    row = row or CalendarJoin(id=claim_id(event))
    row.starts_at = event_start(event)
    row.status = "pending"
    row.summary = details
    row.approved_account_id = None
    row.approval_snapshot = None
    session.add(row)
    await session.flush()
    return True


async def sync_queue(events: list[dict], now: datetime) -> None:
    """Graph görünümünü yansıtır; yalnız maskelenmiş bağlantı ve özeti saklanır."""
    if get_settings().calendar_join_policy == "kapalı":
        return
    events = [
        e
        for e in events
        if eligible(e) and now - timedelta(minutes=5) < event_start(e) <= now + timedelta(days=1)
    ]
    by_id = {claim_id(e): e for e in events}
    async with guarded() as session:
        guard = await session.get(CalendarJoinGuard, 1)
        guard.queue_limited = False
        guard.last_refresh_at = now
        rows = (
            await session.scalars(
                select(CalendarJoin).where(CalendarJoin.status.in_(("pending", "approved")))
            )
        ).all()
        for row in rows:
            event = by_id.get(row.id)
            if event is None:
                row.status = "unavailable"
                row.approval_snapshot = None
                row.approved_account_id = None
            elif (
                row.status == "approved"
                and row.approval_snapshot != approval_snapshot(summary(event))
            ) or (
                row.summary
                and row.summary["organizer_email"] != organizer_info(event)["organizer_email"]
            ):
                row.status = "unavailable"
                row.approval_snapshot = None
                row.approved_account_id = None
                row.summary = summary(event, REAPPROVAL_REASON)
            else:
                row.starts_at = event_start(event)
                reason = (row.summary or {}).get("reason", "")
                if reason != REAPPROVAL_REASON and decision(event) != "otomatik":
                    reason = ""
                row.summary = summary(event, reason)
        await session.flush()
        # Aynı Graph sayfasındaki dış davetler iç/güvenilir davetlerin önüne geçmez.
        for event in sorted(events, key=lambda e: not _priority(organizer_info(e))):
            row = await session.get(CalendarJoin, claim_id(event))
            if row is not None and row.status == "unavailable":
                await _pending(session, event, REAPPROVAL_REASON, row)
            elif row is not None and row.status == "evicted":
                await _pending(session, event, (row.summary or {}).get("reason", ""), row)
            elif decision(event) == "onay" and row is None:
                await _pending(session, event)


async def approval_queue(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    status = await queue_status(now)
    if status["disabled"]:
        return {**status, "meetings": []}
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(CalendarJoin)
                .where(
                    CalendarJoin.status.in_(("pending", "approved")),
                    CalendarJoin.starts_at > now - timedelta(minutes=5),
                    CalendarJoin.starts_at <= now + timedelta(days=1),
                )
                .order_by(CalendarJoin.starts_at)
            )
        ).all()
        return {
            **status,
            "meetings": [
                {
                    **{k: v for k, v in r.summary.items() if k != "join_url_hash"},
                    "id": r.id,
                    "status": r.status,
                }
                for r in rows
                if r.summary
            ],
        }


async def queue_status(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    disabled = get_settings().calendar_join_policy == "kapalı"
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(CalendarJoin)
            .where(
                CalendarJoin.status == "pending",
                CalendarJoin.starts_at > now - timedelta(minutes=5),
                CalendarJoin.starts_at <= now + timedelta(days=1),
            )
        )
        guard = await session.get(CalendarJoinGuard, 1)
        full = not disabled and bool(
            count >= get_settings().calendar_max_pending or (guard and guard.queue_limited)
        )
        return {
            "pending": count,
            "limit": get_settings().calendar_max_pending,
            "queue_full": full,
            "warning": "Kuyruk dolu" if full else "",
            "disabled": disabled,
            "reason": CLOSED_REASON if disabled else "",
            "last_refresh_at": guard.last_refresh_at if guard else None,
        }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def refresh_queue(now: datetime | None = None) -> dict:
    """Yönetici yenilemeleri arasında 15 saniye bekler; arka plan kontrolü sınırı etkilemez."""
    now = now or datetime.now(UTC)
    if get_settings().calendar_join_policy == "kapalı":
        return await approval_queue(now)
    if configured():
        async with guarded() as session:
            guard = await session.get(CalendarJoinGuard, 1)
            interval = MANUAL_REFRESH_INTERVAL_S
            if (
                guard.last_manual_refresh_at
                and (now - _utc(guard.last_manual_refresh_at)).total_seconds() < interval
            ):
                raise HTTPException(
                    429, f"Takvim yenilemeleri arasında en az {interval} saniye bekleyin"
                )
            guard.last_manual_refresh_at = now
        await sync_queue(await calendar_events(now), now)
    return await approval_queue(now)


async def resolve(event_key: str, approve: bool, account_id: int | None) -> None:
    async with guarded() as session:
        row = await session.get(CalendarJoin, event_key)
        if row is None:
            raise HTTPException(404, "Takvim daveti bulunamadı")
        if row.status not in ("pending", "approved"):
            raise HTTPException(409, "Bu oluşum için karar zaten verildi veya davet geçersiz")
        if approve and row.status != "pending":
            raise HTTPException(409, "Bu oluşum zaten onaylandı")
        if approve and get_settings().calendar_join_policy == "kapalı":
            raise HTTPException(409, CLOSED_REASON)
        if approve and (not row.summary or not row.summary.get("join_url_hash")):
            raise HTTPException(409, "Davet özeti eski; takvimi yenileyin")
        row.status = "approved" if approve else "rejected"
        row.approved_account_id = account_id if approve else None
        row.approval_snapshot = approval_snapshot(row.summary) if approve else None
        session.add(
            AuditLog(
                actor=f"panel:{account_id}" if account_id else "admin",
                action="calendar.approved" if approve else "calendar.rejected",
                detail={"event_key": event_key, "account_id": account_id},
            )
        )
    await notification_hook(event_key, "approved" if approve else "rejected")


async def reserve(event: dict, now: datetime) -> tuple[bool, int | None]:
    async with guarded() as session:
        row = await session.get(CalendarJoin, claim_id(event))
        if row and row.status not in ("pending", "approved"):
            return False, None
        approved = row is not None and row.status == "approved"
        if approved and row.approval_snapshot != approval_snapshot(summary(event)):
            row.status = "unavailable"
            row.approval_snapshot = None
            row.approved_account_id = None
            row.summary = summary(event, REAPPROVAL_REASON)
            await session.flush()
            await _pending(session, event, REAPPROVAL_REASON, row)
            return False, None
        if (
            row
            and row.status == "pending"
            and (row.summary or {}).get("reason") == REAPPROVAL_REASON
        ):
            return False, None
        if decision(event) == "kapalı" or (not approved and decision(event) != "otomatik"):
            return False, None
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if not approved:
            used = await session.scalar(
                select(func.count()).select_from(CalendarJoin).where(CalendarJoin.auto_day == day)
            )
            if used >= get_settings().calendar_daily_autojoins:
                if row is None:
                    await _pending(session, event, "Günlük otomatik katılım sınırı doldu")
                else:
                    row.summary = summary(event, "Günlük otomatik katılım sınırı doldu")
                return False, None
        if row is None:
            row = CalendarJoin(id=claim_id(event), starts_at=event_start(event))
            session.add(row)
        row.status = "claimed"
        row.auto_day = None if approved else day
        owner = row.approved_account_id if approved else None
        if owner is None:
            email = organizer_info(event)["organizer_email"]
            matches = []
            for account in await session.scalars(select(PanelAccount).where(PanelAccount.enabled)):
                try:
                    if normalize_email(account.email) == email:
                        matches.append(account.id)
                except ValueError:
                    continue
            if len(matches) == 1:
                owner = matches[0]
        return True, owner


async def calendar_events(now: datetime | None = None) -> list[dict]:
    if get_settings().calendar_join_policy == "kapalı" or not configured():
        return []
    now = now or datetime.now(UTC)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        events = await MicrosoftGraph(client).collection(
            f"/users/{quote(get_settings().m365_mailbox, safe='')}/calendarView",
            {
                "startDateTime": (now - timedelta(minutes=5)).isoformat(),
                "endDateTime": (now + timedelta(days=1)).isoformat(),
                "$select": "id,subject,start,end,attendees,organizer,onlineMeeting,"
                "onlineMeetingUrl,location,"
                "isCancelled,isAllDay,responseStatus",
                "$top": "100",
            },
        )
    return [e for e in events if eligible(e)]


def claim_id(event: dict) -> str:
    # Graph ImmutableId ve posta kutusu her yinelenen oluşumu ayrı tutar.
    value = get_settings().m365_mailbox.strip().casefold() + "\0" + event["id"]
    return hashlib.sha256(value.encode()).hexdigest()


async def claim(event: dict) -> bool:
    async with write_scope() as session:
        result = await session.execute(
            _insert(session)(CalendarJoin)
            .values(id=claim_id(event), starts_at=event_start(event))
            .on_conflict_do_nothing(index_elements=["id"])
        )
        return result.rowcount == 1


async def set_result(event: dict, status: str, meeting_id: str | None = None) -> None:
    async with write_scope() as session:
        await session.execute(
            update(CalendarJoin)
            .where(CalendarJoin.id == claim_id(event))
            .values(status=status, meeting_id=meeting_id)
        )


async def poll_once(now: datetime | None = None) -> int:
    if get_settings().calendar_join_policy == "kapalı" or not configured():
        return 0
    from katip.api.routes.meetings import JoinRequest, start_meeting
    from katip.pipeline.session import registry

    now = now or datetime.now(UTC)
    joined = 0
    events = await calendar_events(now)
    await sync_queue(events, now)
    for event in events:
        # Her kontrolde güncel oluşum kullanılır; kayıtlı eski bağlantı çalıştırılmaz.
        if not eligible(event):
            continue
        start = event_start(event)
        if not start <= now < start + timedelta(minutes=5):
            continue
        claimed, owner = await reserve(event, now)
        if not claimed:
            continue
        url = event_url(event)
        active = [await repo.get_meeting(s.session_id) for s in registry.all()]
        if any(m and m.url == url for m in active):
            await set_result(event, "already_active")
            continue
        identities = []
        for entry in [event.get("organizer") or {}, *event.get("attendees", [])]:
            address = entry.get("emailAddress") or {}
            with contextlib.suppress(ValueError):
                identity = ParticipantIdentity(
                    name=address.get("name") or address.get("address", ""),
                    email=address.get("address", ""),
                )
                if identity.email != get_settings().m365_mailbox.strip().casefold():
                    identities.append(identity)
        try:
            result = await start_meeting(
                JoinRequest(
                    url=url,
                    title=str(event.get("subject") or "")[:200],
                ),
                owner_account_id=owner,
            )
        except HTTPException as exc:
            if exc.status_code == 429:
                # Kapasite kontrolü bot başlamadan yapılır; onayı koruyarak yeniden dene.
                async with guarded() as session:
                    row = await session.get(CalendarJoin, claim_id(event))
                    if row.auto_day is not None:
                        await session.delete(row)
                    else:
                        row.status = "approved"
            else:
                await set_result(event, "failed")
            continue
        except Exception:
            await set_result(event, "failed")
            log.warning("takvim katılımı başarısız", event_key=claim_id(event))
            continue
        await set_result(event, "joined", result.session_id)
        session = registry.get(result.session_id)
        if session:
            # Davetliler kimlik adayıdır; panelce doğrulanmış izin sağlamaz.
            await session.add_identities(identities, "calendar")
        await repo.audit(
            "calendar",
            "calendar.joined",
            meeting_id=result.session_id,
            event_key=claim_id(event),
            owner_account_id=owner,
        )
        await notification_hook(claim_id(event), "joined")
        joined += 1
    return joined


async def run_forever() -> None:
    while True:
        try:
            await poll_once()
        except Exception as exc:
            # Graph içeriği, toplantı parolası ve token adresi loga yazılmaz.
            log.warning("takvim kontrolü başarısız", error_type=type(exc).__name__)
        await asyncio.sleep(get_settings().calendar_poll_s)


async def recent_claims() -> list[dict]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(CalendarJoin).order_by(CalendarJoin.created_at.desc()).limit(50)
                )
            )
            .scalars()
            .all()
        )
        return [
            {"starts_at": r.starts_at, "status": r.status, "meeting_id": r.meeting_id} for r in rows
        ]
