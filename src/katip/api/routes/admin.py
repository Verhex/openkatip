"""Yönetim paneli uçları (yalnız yönetici): durum, sağlık, ayarlar, geçmiş, logo."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ValidationError, field_validator

from katip import runtime_settings
from katip.api.security import PanelAccess, require_admin, require_panel
from katip.bot.supervisor import supervisor
from katip.config import get_settings
from katip.identity import normalize_email
from katip.integrations import calendar
from katip.logging import get_logger
from katip.pipeline.session import registry
from katip.store import repo

log = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])
public_router = APIRouter(tags=["branding"])
panel_router = APIRouter(prefix="/api/admin", tags=["admin"])

_LOGO_MAX_BYTES = 1024 * 1024
_LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
_health_cache: tuple[float, list[dict]] | None = None
_HEALTH_TTL_S = 30.0


@router.get("/teams/package")
async def teams_package():
    from katip.teams.package import build_package

    try:
        package = await asyncio.to_thread(build_package)
    except (ValueError, OSError, StopIteration):
        raise HTTPException(503, "Teams paket ayarları tamamlanmadı") from None
    return Response(
        package,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="katip-teams.zip"',
        },
    )


def _logo_dir() -> Path:
    return Path(get_settings().data_dir) / "branding"


def _find_logo() -> tuple[Path, str] | None:
    for ext, media in _LOGO_TYPES.items():
        path = _logo_dir() / f"logo.{ext}"
        if path.exists():
            return path, media
    return None


def _sniff_image(data: bytes) -> str | None:
    """Dosya içeriğinden tür (uzantı ya da istemcinin söylediği türe güvenilmez)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ------------------------------------------------------------------ genel durum
def _session_row(s) -> dict:
    return {
        "session_id": s.session_id,
        "state": s.state,
        "mode": s.mode,
        "reply_mode": s.reply_mode,
        "paused": s.paused,
        "minutes": round((time.monotonic() - s._created_at) / 60, 1),
        "segments": len([x for x in s.final_segments() if x.speaker != "Katip"]),
        "participants": list(s._roster),
        "target_lang": s.target_lang,
    }


@router.get("/overview")
async def overview() -> dict:
    from katip.workers import jobs

    s = get_settings()
    sessions = []
    accounts = {a.id: a.email for a in await repo.list_panel_accounts()}
    for live in registry.all():
        meeting = await repo.get_meeting(live.session_id)
        usage = await repo.get_meeting_usage(live.session_id)
        sessions.append(
            {
                **_session_row(live),
                "title": meeting.title if meeting else None,
                "owner": accounts.get(meeting.owner_account_id)
                if meeting and meeting.owner_account_id
                else "sistem",
                "platform": meeting.platform if meeting else None,
                "cost_usd": round(
                    (usage.llm_usd or 0) + (usage.stt_usd or 0) + (usage.tts_usd or 0), 4
                )
                if usage
                else 0,
            }
        )
    return {
        "sessions": sessions,
        "bots": {"running": await jobs.count_open(), "limit": s.max_concurrent_bots},
        "today_minutes": round(await repo.meeting_minutes_today(), 1),
        "daily_limit": s.daily_meeting_minutes,
        "version": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "yerel")[:7],
        "company": s.company_name,
        "bot_name": s.bot_display_name,
        "has_logo": _find_logo() is not None,
    }


# ------------------------------------------------------------------ sağlık
async def _check_soniox() -> tuple[bool | None, str]:
    import httpx

    s = get_settings()
    if not s.soniox_api_key:
        return False, "SONIOX_API_KEY tanımlı değil"
    url = s.soniox_endpoint("https://api.soniox.com/v1/models")
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {s.soniox_api_key}"})
    except Exception as exc:
        return False, f"erişilemiyor: {type(exc).__name__}"
    if r.status_code == 200:
        return True, f"anahtar geçerli · bölge {s.soniox_region.upper()} · model {s.soniox_model}"
    if r.status_code in (401, 403):
        return False, "anahtar geçersiz"
    return None, f"beklenmeyen cevap ({r.status_code})"


async def _check_anthropic() -> tuple[bool | None, str]:
    from anthropic import AuthenticationError

    from katip.providers import get_registry

    s = get_settings()
    if not s.anthropic_api_key:
        return False, "ANTHROPIC_API_KEY tanımlı değil"
    try:
        client = get_registry().llm_provider("anthropic").client
        await client.models.retrieve(s.agent_model)
    except AuthenticationError:
        return False, "anahtar geçersiz"
    except Exception as exc:
        return None, f"doğrulanamadı: {type(exc).__name__}"
    return True, f"anahtar geçerli · {s.agent_model}"


async def _check_db() -> tuple[bool | None, str]:
    try:
        await repo.recent_meetings(1)
    except Exception as exc:
        return False, f"hata: {type(exc).__name__}"
    url = get_settings().database_url
    return True, "SQLite (kalıcı birim)" if url.startswith("sqlite") else "Postgres"


def _check_disk() -> tuple[bool | None, str]:
    try:
        usage = shutil.disk_usage(get_settings().data_dir)
    except OSError as exc:
        return False, f"okunamadı: {exc}"
    used = usage.used / usage.total if usage.total else 0
    detail = f"{usage.used / 1e9:.2f} / {usage.total / 1e9:.1f} GB dolu (%{used * 100:.0f})"
    return (used < 0.85) or None, detail


async def health_checks() -> list[dict]:
    from katip.integrations.mail import health as mail_health
    from katip.integrations.microsoft import configured as microsoft_configured
    from katip.store.crypto import available
    from katip.store.migration import state as encryption_state
    from katip.teams.security import health as teams_health

    s = get_settings()
    soniox, anthropic, db = await asyncio.gather(_check_soniox(), _check_anthropic(), _check_db())
    disk = _check_disk()
    used = await repo.meeting_minutes_today()
    retention = await repo.recent_audit(("retention_purge",), limit=1)
    calendar_queue = await calendar.queue_status()
    pending_erasures = await repo.pending_erasure_ids()

    def row(key: str, label: str, result: tuple[bool | None, str]) -> dict:
        return {"key": key, "label": label, "ok": result[0], "detail": result[1]}

    return [
        row("teams", "Teams uygulaması", teams_health()),
        row("mail", "Toplantı notu maili", await mail_health()),
        row(
            "pending_erasure",
            "Tamamlanmamış kişisel veri silme talepleri",
            (
                not pending_erasures,
                f"{len(pending_erasures)} talep bekliyor; otomatik yeniden deneniyor",
            ),
        ),
        row("soniox", "Soniox (yazıya dökme, ses)", soniox),
        row("anthropic", "Anthropic Claude (agent, rapor)", anthropic),
        row("database", "Veritabanı", db),
        row("disk", "Depolama", disk),
        row(
            "data_encryption",
            "Toplantı verisi şifreleme",
            (encryption_state != "başarısız" or None, f"AES-256-GCM etkin · göç {encryption_state}")
            if available()
            else (None, "Veri anahtarı tanımlı değil; toplantı içeriği düz metin saklanıyor"),
        ),
        row(
            "voice_key",
            "Sesle kimlik tanıma",
            (True, "kapalı; katılımcılar e-posta ile eşleştirilir"),
        ),
        row(
            "voice_model",
            "Ses tanıma modeli",
            (True, "kapalı; model yüklenmez"),
        ),
        row(
            "microsoft",
            "Microsoft 365 e-posta kaynağı",
            (
                None,
                "yapılandırıldı; erişimi katılımcı eşleştirme bölümünden kontrol edin"
                if microsoft_configured()
                else "yapılandırılmadı; e-postalar panelden girilebilir",
            ),
        ),
        row(
            "calendar_queue",
            "Takvim onay kuyruğu",
            (
                None if calendar_queue["queue_full"] else True,
                calendar_queue["reason"]
                or calendar_queue["warning"]
                or f"{calendar_queue['pending']} / {calendar_queue['limit']} bekleyen davet",
            ),
        ),
        row(
            "bot",
            "Bot kapasitesi",
            (
                len(supervisor._procs) < s.max_concurrent_bots or None,
                f"{len(supervisor._procs)} / {s.max_concurrent_bots} bot çalışıyor",
            ),
        ),
        row(
            "daily",
            "Günlük süre sınırı",
            (
                used < s.daily_meeting_minutes or None,
                f"bugün {used:.0f} / {s.daily_meeting_minutes} dk",
            ),
        ),
        row(
            "retention",
            "Saklama ve imha",
            (
                True,
                f"{s.retention_days} gün"
                + (
                    f" · son çalışma {retention[0].at:%d.%m %H:%M}"
                    if retention
                    else " · henüz silinecek veri yok"
                ),
            ),
        ),
        row(
            "auth",
            "Giriş güvenliği",
            (
                bool(s.admin_password_hash or await _db_password()) or None,
                "parola tanımlı · Microsoft (Entra) girişi henüz bağlı değil",
            ),
        ),
    ]


async def _db_password() -> bool:
    return bool((await repo.get_app_settings()).get("admin_password_hash"))


@router.get("/health")
async def health(refresh: bool = False) -> dict:
    global _health_cache
    now = time.monotonic()
    if refresh or _health_cache is None or now - _health_cache[0] > _HEALTH_TTL_S:
        _health_cache = (now, await health_checks())
    from katip.providers import get_registry

    providers = get_registry().health()
    for check in _health_cache[1]:
        if check["key"] in providers["providers"]:
            providers["providers"][check["key"]].update(ok=check["ok"], detail=check["detail"])
    return {
        "checked_seconds_ago": round(now - _health_cache[0]),
        "checks": _health_cache[1],
        **providers,
    }


# ------------------------------------------------------------------ ayarlar
@router.get("/settings")
async def get_editable_settings() -> dict:
    return {"values": runtime_settings.current()}


@router.put("/settings")
async def put_editable_settings(
    changes: runtime_settings.EditableSettings,
    access: Annotated[PanelAccess, Depends(require_admin)],
) -> dict:
    actor = f"panel:{access.account_id}" if access.account_id else "admin"
    return {"values": await runtime_settings.update(changes, actor=actor)}


class CalendarOrganizerRequest(BaseModel):
    email: str

    _email = field_validator("email")(normalize_email)


@router.post("/calendar/organizers/trust")
async def trust_calendar_organizer(
    request: CalendarOrganizerRequest,
    access: Annotated[PanelAccess, Depends(require_admin)],
) -> dict:
    actor = f"panel:{access.account_id}" if access.account_id else "admin"
    try:
        values = await runtime_settings.trust_calendar_organizer(request.email, actor=actor)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, "Güven listesi geçersiz veya e-posta sınırı doldu") from exc
    return {"values": values}


# ------------------------------------------------------------------ geçmiş ve kayıtlar
@panel_router.get("/meetings")
async def meetings(
    access: Annotated[PanelAccess, Depends(require_panel)],
    limit: int = 30,
    account_id: int | None = None,
) -> dict:
    if account_id is not None and access.role != "admin":
        raise HTTPException(403, "Kullanıcı filtresi yalnız yöneticiye açıktır")
    managed = await access.meeting_ids("manage")
    rows = []
    for m in await repo.recent_meetings(
        min(max(limit, 1), 100),
        account_id=account_id if account_id is not None else access.account_id,
        role="operator" if account_id is not None else access.role,
    ):
        minutes = None
        if m.started_at and m.ended_at:
            minutes = round((m.ended_at - m.started_at).total_seconds() / 60)
        has_report = bool((m.documents or {}).get("pdf"))
        rows.append(
            {
                "id": m.id,
                "can_manage": managed is None or m.id in managed,
                "owner_account_id": m.owner_account_id,
                "title": m.title or (m.report or {}).get("title"),
                "platform": m.platform,
                "state": m.state,
                "started_at": m.started_at.isoformat() if m.started_at else None,
                "minutes": minutes,
                "report_pdf": f"/api/meetings/{m.id}/report.pdf" if has_report else None,
                "report_docx": f"/api/meetings/{m.id}/report.docx" if has_report else None,
            }
        )
    return {"meetings": rows}


@router.get("/logins")
async def logins() -> dict:
    actions = (
        "admin_login",
        "admin_login_failed",
        "admin_login_locked",
        "admin_password_changed",
        "admin_logout_all",
        "settings_changed",
        "calendar.approved",
        "calendar.rejected",
        "calendar.joined",
        "logo_changed",
    )
    return {
        "events": [
            {"at": e.at.isoformat(), "action": e.action, "detail": e.detail}
            for e in await repo.recent_audit(actions, limit=40)
        ]
    }


@router.get("/admin-sessions")
async def admin_sessions() -> dict:
    return {
        "sessions": [
            {
                "created_at": x.created_at.isoformat(),
                "last_seen": x.last_seen.isoformat(),
                "client": x.client,
                "user_agent": x.user_agent,
            }
            for x in await repo.list_admin_sessions()
        ]
    }


# ------------------------------------------------------------------ logo
@router.post("/branding/logo")
async def upload_logo(request: Request) -> dict:
    data = await request.body()
    if not data or len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Logo boş ya da 1 MB'tan büyük")
    kind = _sniff_image(data)
    if kind is None:
        raise HTTPException(status_code=415, detail="Yalnız PNG, JPEG ya da WebP yüklenebilir")
    folder = _logo_dir()

    def write() -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for ext in _LOGO_TYPES:
            (folder / f"logo.{ext}").unlink(missing_ok=True)
        (folder / f"logo.{kind}").write_bytes(data)

    await asyncio.to_thread(write)
    await repo.audit("admin", "logo_changed", type=kind, bytes=len(data))
    return {"ok": True, "type": kind}


@router.delete("/branding/logo")
async def delete_logo() -> dict:
    def remove() -> None:
        for ext in _LOGO_TYPES:
            (_logo_dir() / f"logo.{ext}").unlink(missing_ok=True)

    await asyncio.to_thread(remove)
    await repo.audit("admin", "logo_changed", removed=True)
    return {"ok": True}


@public_router.get("/branding/logo", include_in_schema=False)
async def logo() -> FileResponse:
    found = _find_logo()
    if found is None:
        raise HTTPException(status_code=404, detail="logo yok")
    path, media = found
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache"})
