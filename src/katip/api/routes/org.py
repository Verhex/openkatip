"""Şirket şeması ve kişiler.

Yazma ve silme uçları yönetici token'ı ister (`KATIP_ADMIN_TOKEN`, `Authorization: Bearer`).
Token tanımlı değilse bu uçlar kapalıdır — yanlışlıkla açık bırakılmasınlar diye.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from katip.api.security import require_admin
from katip.store import repo
from katip.store.org import OrgChart, apply_org_chart

router = APIRouter(prefix="/api", tags=["org"])


__all__ = ["require_admin", "router"]


class PersonOut(BaseModel):
    id: int
    name: str
    email: str | None
    role: str
    title: str | None
    department: str | None
    aliases: list[str]
    voice_enrolled: bool


@router.get("/people", response_model=list[PersonOut], dependencies=[Depends(require_admin)])
async def people() -> list[PersonOut]:
    enrolled = await repo.enrolled_person_ids()
    return [
        PersonOut(
            id=p.id,
            name=p.display_name,
            email=p.email,
            role=p.role,
            title=p.title,
            department=p.department,
            aliases=p.aliases or [],
            voice_enrolled=p.id in enrolled,
        )
        for p in await repo.list_people()
    ]


@router.put("/org", dependencies=[Depends(require_admin)])
async def put_org(yaml_text: str = Body(..., media_type="application/yaml")) -> dict[str, int]:
    try:
        chart = OrgChart.from_yaml(yaml_text)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"people": await apply_org_chart(chart)}


@router.delete("/people/{person_id}", dependencies=[Depends(require_admin)])
async def forget(person_id: int) -> dict:
    """Yönetici panelinden "unut": ses izi + profil silinir, geçmişte anonimleşir."""
    person = await repo.get_person(person_id)
    failures: list[str] = []
    if person is None:
        raise HTTPException(status_code=404, detail="kişi bulunamadı")
    completed = await repo.forget_person(person_id, requested_by="admin-api", failures=failures)
    from katip.pipeline.session import registry

    for session in registry.all():
        session.on_person_forgotten(person_id, person.display_name)
    return {"forgotten": completed, "pending": not completed, "failed_meetings": failures}
