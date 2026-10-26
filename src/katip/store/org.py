"""Şirket şeması: kim kimdir, hangi rolde, kime bağlı.

Kullanım alanları: rapordaki aksiyonların sahibine unvanıyla atanması, toplantı
sonrası mailin kime gideceği, ekranda görünen adın ("deniz.s") gerçek kişiye
bağlanması, admin rolünün tanımlanması.

Biçim (YAML) — şirket adı rapor başlığı için KATIP_COMPANY_NAME ile verilir:

    people:
      - name: Deniz Kaya
        email: deniz@example.com
        title: Kurucu
        department: Yönetim
        role: admin
        aliases: [Deniz, deniz.s]
      - name: Ayşe Yılmaz
        email: ayse@example.com
        title: Pazarlama Müdürü
        department: Pazarlama
        manager: Deniz Kaya
"""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from katip.store.db import session_scope, write_scope
from katip.store.models import Person, Role


class PersonSpec(BaseModel):
    name: str
    email: str | None = None
    title: str | None = None
    department: str | None = None
    role: Role = Role.MEMBER
    manager: str | None = None
    aliases: list[str] = Field(default_factory=list)


class OrgChart(BaseModel):
    people: list[PersonSpec]

    @model_validator(mode="after")
    def _check(self) -> OrgChart:
        names = [p.name for p in self.people]
        if len(names) != len(set(names)):
            raise ValueError("şema içinde aynı isim birden fazla kez geçiyor")
        unknown = {p.manager for p in self.people if p.manager and p.manager not in names}
        if unknown:
            raise ValueError(f"şemada olmayan yönetici: {', '.join(sorted(unknown))}")
        return self

    @classmethod
    def from_yaml(cls, text: str) -> OrgChart:
        return cls.model_validate(yaml.safe_load(text) or {})


async def apply_org_chart(chart: OrgChart) -> int:
    """Şemayı depoya uygular (adına veya e-postasına göre günceller, yoksa ekler)."""
    async with write_scope() as s:
        existing = list(await s.scalars(select(Person)))
        by_key: dict[str, Person] = {}
        for person in existing:
            by_key[person.display_name.casefold()] = person
            if person.email:
                by_key[person.email.casefold()] = person

        rows: dict[str, Person] = {}
        for spec in chart.people:
            named = by_key.get(spec.name.casefold())
            if spec.email:
                person = by_key.get(spec.email.casefold())
                if person is None and named and not named.email:
                    person = named
            else:
                person = named
            if person is None:
                person = Person(display_name=spec.name)
                s.add(person)
            person.display_name = spec.name
            person.email = spec.email
            person.title = spec.title
            person.department = spec.department
            person.role = spec.role
            person.aliases = spec.aliases
            rows[spec.name] = person
        await s.flush()

        for spec in chart.people:
            rows[spec.name].manager_id = rows[spec.manager].id if spec.manager else None
        return len(rows)


async def org_context() -> str:
    """Rapor/agent prompt'una eklenecek kısa şema özeti."""
    async with session_scope() as s:
        people = list(
            await s.scalars(select(Person).order_by(Person.department, Person.display_name))
        )
    if not people:
        return ""
    by_id = {p.id: p for p in people}
    lines = []
    for p in people:
        parts = [p.display_name]
        if p.title:
            parts.append(p.title)
        if p.department:
            parts.append(p.department)
        if p.manager_id and p.manager_id in by_id:
            parts.append(f"yöneticisi: {by_id[p.manager_id].display_name}")
        if p.aliases:
            parts.append(f"toplantıda görünen adları: {', '.join(p.aliases)}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)
