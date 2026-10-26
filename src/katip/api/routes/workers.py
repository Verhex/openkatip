from __future__ import annotations

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from katip.api.security import PanelAccess, require_meeting_admin, require_panel
from katip.bot.supervisor import bot_environment
from katip.config import get_settings
from katip.workers import jobs

router = APIRouter(prefix="/api/workers", tags=["workers"])


async def manager_identity(
    x_katip_worker_id: str = Header(default=""), authorization: str = Header(default="")
) -> str:
    expected = get_settings().worker_manager_keys.get(x_katip_worker_id)
    given = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if not expected or len(expected) < 32 or not secrets.compare_digest(expected, given):
        raise HTTPException(401, "Worker kimliği geçersiz")
    return x_katip_worker_id


class LeaseRequest(BaseModel):
    session_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    lease_id: str = Field(pattern=r"^[a-f0-9]{48}$")


class FinishRequest(LeaseRequest):
    exit_code: int = Field(ge=-255, le=255)
    stop_confirmed: bool = False


@router.post("/claim")
async def claim(worker_id: str = Depends(manager_identity)) -> dict:
    result = await jobs.claim(worker_id)
    if result is None:
        return {"job": None}
    row, token = result
    env = {key: value for key, value in bot_environment(token).items() if key.startswith("KATIP_")}
    # Remote host owns ingest routing; no API filesystem paths leave the control plane.
    env.pop("KATIP_DATA_DIR", None)
    env.pop("KATIP_INGEST_WS_URL", None)
    env["KATIP_BOT_SANDBOX"] = "on"
    env["KATIP_BOT_SITE_ISOLATION"] = "true"
    env["KATIP_BOT_BLOCK_PRIVATE_NETWORK"] = "true"
    return {
        "job": {
            "session_id": row.meeting_id,
            "lease_id": row.lease_id,
            "url": row.spec["url"],
            "max_minutes": row.spec["max_minutes"],
            "lease_s": get_settings().worker_lease_s,
            "environment": env,
        }
    }


@router.get("/owned")
async def owned(worker_id: str = Depends(manager_identity)) -> list[dict]:
    # Restart reconciliation also recovers a claim whose HTTP response was lost.
    return [
        {"session_id": row.meeting_id, "lease_id": row.lease_id}
        for row in await jobs.listing(open_only=True)
        if row.worker_id == worker_id and row.status in ("running", "stopping", "lost")
    ]


@router.post("/heartbeat")
async def heartbeat(req: LeaseRequest, worker_id: str = Depends(manager_identity)) -> dict:
    keep = await jobs.heartbeat(req.session_id, worker_id, req.lease_id)
    return {"continue": keep, "lease_s": get_settings().worker_lease_s}


@router.post("/finish")
async def finish(req: FinishRequest, worker_id: str = Depends(manager_identity)) -> dict:
    if not req.stop_confirmed:
        raise HTTPException(409, "Worker kapanışı doğrulanmadı")
    if not await jobs.finish(req.session_id, worker_id, req.lease_id, req.exit_code):
        raise HTTPException(409, "Worker sahipliği geçersiz")
    return {"ok": True}


@router.get("/jobs")
async def listing(access: Annotated[PanelAccess, Depends(require_panel)]) -> list[dict]:
    allowed = await access.meeting_ids()
    return [
        jobs.public_status(row)
        for row in await jobs.listing()
        if allowed is None or row.meeting_id in allowed
    ]


class ResolveRequest(BaseModel):
    confirmation: Literal["worker_stop_verified"]


@router.post("/jobs/{session_id}/resolve", dependencies=[Depends(require_meeting_admin)])
async def resolve(session_id: str, req: ResolveRequest) -> dict:
    if not await jobs.resolve_unknown(session_id):
        raise HTTPException(409, "Yalnızca kapanışı belirsiz iş elle kapatılabilir")
    return {"manually_closed": True, "stop_confirmed": False}
