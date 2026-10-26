"""Panelden toplantının e-posta listesini ve isim eşleştirmelerini yönetir."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from katip.api.security import require_meeting_admin, require_meeting_read
from katip.identity import ParticipantIdentity
from katip.pipeline.session import registry
from katip.store import repo

router = APIRouter(prefix="/api/meetings", tags=["identity"])


async def _exists(meeting_id: str) -> None:
    if await repo.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="Toplantı bulunamadı")


@router.get("/{meeting_id}/participants", dependencies=[Depends(require_meeting_read)])
async def participants(meeting_id: str) -> dict:
    await _exists(meeting_id)
    observed = {
        p.name: {"name": p.name, "email": p.email, "source": p.identity_source}
        for p in await repo.list_participants(meeting_id)
    }
    session = registry.get(meeting_id)
    if session:
        names = session.human_roster() + [s.speaker for s in session.final_segments()]
    else:
        meeting = await repo.get_meeting(meeting_id)
        names = [s.speaker_name for s in meeting.segments]
    for name in names:
        if name and name not in ("Katip", "Anonim katılımcı"):
            observed.setdefault(name, {"name": name, "email": None, "source": "unknown"})
    return {
        "participants": list(observed.values()),
        "identities": [
            {"name": i.name, "email": i.email, "source": i.source}
            for i in await repo.list_meeting_identities(meeting_id)
        ],
    }


class IdentityList(BaseModel):
    participants: list[ParticipantIdentity] = Field(max_length=200)


@router.put("/{meeting_id}/participants", dependencies=[Depends(require_meeting_admin)])
async def import_participants(meeting_id: str, req: IdentityList) -> dict:
    await _exists(meeting_id)
    session = registry.get(meeting_id)
    if session:
        await session.add_identities(req.participants, "panel")
    else:
        for identity in req.participants:
            await repo.save_meeting_identity(meeting_id, identity, "panel")
    await repo.audit(
        "admin", "meeting_emails_imported", meeting_id=meeting_id, count=len(req.participants)
    )
    return {"imported": len(req.participants)}


@router.put("/{meeting_id}/participants/binding", dependencies=[Depends(require_meeting_admin)])
async def bind(meeting_id: str, identity: ParticipantIdentity) -> dict:
    await _exists(meeting_id)
    known_names = {p.name for p in await repo.list_participants(meeting_id)}
    session = registry.get(meeting_id)
    if session:
        known_names.update(session.human_roster())
        known_names.update(s.speaker for s in session.final_segments())
    else:
        meeting = await repo.get_meeting(meeting_id)
        known_names.update(s.speaker_name for s in meeting.segments)
    if identity.name not in known_names:
        raise HTTPException(status_code=422, detail="Toplantıda gözlenen katılımcı adını seçin")
    if identity.name in ("Katip", "Anonim katılımcı"):
        raise HTTPException(status_code=422, detail="Bu konuşmacı eşleştirilemez")
    if session:
        await session.bind_identity(identity.name, identity)
    else:
        await repo.save_meeting_identity(meeting_id, identity, "panel")
        await repo.bind_participant(meeting_id, identity.name, identity, "panel")
    await repo.audit("admin", "participant_bound", meeting_id=meeting_id)
    return {"bound": True}


@router.post(
    "/{meeting_id}/participants/microsoft-sync", dependencies=[Depends(require_meeting_admin)]
)
async def microsoft_sync(meeting_id: str) -> dict:
    await _exists(meeting_id)
    from katip.integrations.microsoft import sync_identities

    return await sync_identities(meeting_id)
