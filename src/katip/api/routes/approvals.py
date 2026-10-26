"""Yönetici onayları: kritik agent işleri (ör. kalıcı talimat) panelden onaylanır."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from katip.agent import controller
from katip.api.auth import current_admin_session
from katip.api.routes.org import require_admin
from katip.store import repo

router = APIRouter(
    prefix="/api/approvals", tags=["approvals"], dependencies=[Depends(require_admin)]
)


@router.get("")
async def pending() -> list[dict]:
    return [
        {"id": a.id, "session_id": a.session_id, "description": a.description}
        for a in controller.APPROVALS.values()
    ]


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, request: Request) -> dict[str, bool]:
    if not await controller.approve(approval_id):
        raise HTTPException(status_code=404, detail="onay bulunamadı")
    session = await current_admin_session(request)
    await repo.audit(
        "admin",
        "directive_approved",
        approval_id=approval_id,
        account_id=session.account_id if session else None,
    )
    return {"approved": True}


@router.post("/{approval_id}/reject")
async def reject(approval_id: str, request: Request) -> dict[str, bool]:
    if not controller.reject(approval_id):
        raise HTTPException(status_code=404, detail="onay bulunamadı")
    session = await current_admin_session(request)
    await repo.audit(
        "admin",
        "directive_rejected",
        approval_id=approval_id,
        account_id=session.account_id if session else None,
    )
    return {"rejected": True}
