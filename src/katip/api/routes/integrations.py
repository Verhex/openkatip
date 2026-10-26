"""Takvim araçları; posta kutusu yalnız yöneticiye açıktır."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from katip.api.security import PanelAccess, require_admin
from katip.bot.platforms.base import platform_for
from katip.config import get_settings
from katip.integrations import calendar, microsoft

router = APIRouter(
    prefix="/api/integrations", tags=["integrations"], dependencies=[Depends(require_admin)]
)


@router.get("/calendar")
async def calendar_status() -> dict:
    s = get_settings()
    return {
        "configured": microsoft.configured(),
        "autojoin": s.calendar_join_policy in ("güvenilir", "hepsi"),
        "join_policy": s.calendar_join_policy,
        "policy": s.calendar_join_policy,
        "daily_autojoins": s.calendar_daily_autojoins,
        "max_pending": s.calendar_max_pending,
        "mailbox": s.m365_mailbox,
        "poll_seconds": s.calendar_poll_s,
        "organizer_domains": s.calendar_organizer_domains,
        "organizer_emails": s.calendar_organizer_emails,
        "recent_joins": await calendar.recent_claims(),
        "queue": await calendar.queue_status(),
    }


@router.get("/calendar/queue")
async def queue() -> dict:
    return await calendar.approval_queue()


@router.post("/calendar/refresh", dependencies=[Depends(require_admin)])
async def refresh() -> dict:
    try:
        return await calendar.refresh_queue()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Microsoft takvimine erişilemedi") from exc


@router.post("/calendar/queue/{event_key}/approve")
async def approve(event_key: str, access: Annotated[PanelAccess, Depends(require_admin)]) -> dict:
    await calendar.resolve(event_key, True, access.account_id)
    return {"status": "approved"}


@router.post("/calendar/queue/{event_key}/reject")
async def reject(event_key: str, access: Annotated[PanelAccess, Depends(require_admin)]) -> dict:
    await calendar.resolve(event_key, False, access.account_id)
    return {"status": "rejected"}


@router.get("/calendar/upcoming")
async def upcoming() -> dict:
    try:
        events = await calendar.calendar_events()
    except Exception as exc:
        raise HTTPException(502, "Microsoft takvimine erişilemedi") from exc
    return {
        "configured": microsoft.configured(),
        "meetings": [
            {
                "title": str(e.get("subject") or "")[:200],
                "starts_at": calendar.event_start(e),
                "platform": platform_for(calendar.event_url(e)).platform.value,
            }
            for e in events
        ],
    }
