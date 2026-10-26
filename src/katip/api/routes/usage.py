"""Kullanım ve maliyet raporu (yalnız yönetici): toplantı başına STT, TTS ve Claude.

Tutarlar `katip/usage.py`'deki resmî fiyatlarla hesaplanan tahminlerdir; kesin fatura
sağlayıcı konsollarındadır (Anthropic Console, Soniox Console, Railway Usage).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from katip.api.security import PanelAccess, require_meeting_read, require_panel
from katip.store import repo

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _row(meeting, u) -> dict:
    total = (u.llm_usd or 0) + (u.stt_usd or 0) + (u.tts_usd or 0)
    return {
        "meeting_id": meeting.id,
        "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
        "title": (meeting.report or {}).get("title"),
        "minutes": round((u.stt_seconds or 0) / 60, 1),
        "llm_calls": u.llm_calls,
        "providers": u.providers or {},
        "tokens": {
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_read": u.cache_read_tokens,
            "cache_write": u.cache_write_tokens,
        },
        "usd": {
            "claude": round(u.llm_usd or 0, 4),
            "stt": round(u.stt_usd or 0, 4),
            "tts": round(u.tts_usd or 0, 4),
            "total": round(total, 4),
        },
        "usd_per_hour": round(total / (u.stt_seconds / 3600), 3) if u.stt_seconds else None,
    }


@router.get("")
async def usage_summary(
    access: Annotated[PanelAccess, Depends(require_panel)], days: int = Query(30, ge=1, le=366)
) -> dict:
    rows = [
        _row(m, u)
        for m, u in await repo.usage_report(days, account_id=access.account_id, role=access.role)
    ]
    totals = {
        "meetings": len(rows),
        "minutes": round(sum(r["minutes"] for r in rows), 1),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "usd": {
            k: round(sum(r["usd"][k] for r in rows), 4) for k in ("claude", "stt", "tts", "total")
        },
    }
    return {"days": days, "totals": totals, "meetings": rows}


@router.get("/{session_id}", dependencies=[Depends(require_meeting_read)])
async def meeting_usage(session_id: str) -> dict:
    meeting = await repo.get_meeting(session_id)
    usage = await repo.get_meeting_usage(session_id)
    if meeting is None or usage is None:
        raise HTTPException(404, "Kullanım kaydı bulunamadı")
    return _row(meeting, usage)
