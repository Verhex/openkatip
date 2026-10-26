"""Depo: şirket şeması, rıza zorunluluğu, "beni unut" anonimleştirmesi, denetim kaydı."""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select

from katip.events import Segment
from katip.store import repo
from katip.store.db import session_scope
from katip.store.models import ANONYMOUS_SPEAKER, AuditLog, Consent, Voiceprint
from katip.store.org import OrgChart, apply_org_chart, org_context

ORG_YAML = """
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
    aliases: [Ayşe]
"""


def seg(sid: str, speaker: str, text: str, start: float) -> Segment:
    return Segment(
        id=sid,
        session_id="m1",
        speaker=speaker,
        start_s=start,
        end_s=start + 2,
        text=text,
        lang="tr",
        is_final=True,
    )


async def test_org_chart_applies_and_updates(database) -> None:
    assert await apply_org_chart(OrgChart.from_yaml(ORG_YAML)) == 2
    ayse = await repo.find_person_by_name("Ayşe")  # takma adla bulunur
    assert ayse is not None and ayse.title == "Pazarlama Müdürü"
    boss = await repo.find_person_by_name("deniz.s")
    assert boss is not None and boss.role == "admin"
    assert ayse.manager_id == boss.id

    # Aynı şema tekrar uygulanınca kopya oluşmaz, güncellenir.
    await apply_org_chart(OrgChart.from_yaml(ORG_YAML.replace("Pazarlama Müdürü", "CMO")))
    people = await repo.list_people()
    assert len(people) == 2
    assert (await repo.find_person_by_name("Ayşe Yılmaz")).title == "CMO"

    ctx = await org_context()
    assert "Ayşe Yılmaz — CMO — Pazarlama — yöneticisi: Deniz Kaya" in ctx


def test_org_chart_rejects_unknown_manager() -> None:
    with pytest.raises(ValueError, match="yönetici"):
        OrgChart.from_yaml("people:\n  - name: A\n    manager: Yok\n")


async def test_voiceprint_requires_explicit_consent(database) -> None:
    person = await repo.ensure_person("Mehmet")
    vec = np.ones(256, dtype=np.float32)
    with pytest.raises(PermissionError):
        await repo.save_voiceprint(person.id, vec, model="m", speech_seconds=10, samples=3)

    await repo.add_consent(person.id, "voiceprint", "sesli: 'onaylıyorum'", "m1")
    await repo.save_voiceprint(person.id, vec, model="m", speech_seconds=10, samples=3)
    loaded = await repo.load_voiceprints("m")
    assert [(pid, name) for pid, name, _, _ in loaded] == [(person.id, "Mehmet")]
    assert np.allclose(loaded[0][3], vec)
    assert await repo.load_voiceprints("baska-model") == []


async def test_forget_deletes_biometrics_and_anonymizes_history(database) -> None:
    await apply_org_chart(OrgChart.from_yaml(ORG_YAML))
    ayse = await repo.find_person_by_name("Ayşe Yılmaz")
    await repo.add_consent(ayse.id, "voiceprint", "sesli", "m1")
    await repo.save_voiceprint(
        ayse.id, np.ones(8, np.float32), model="m", speech_seconds=5, samples=1
    )

    await repo.upsert_meeting("m1", platform="teams")
    await repo.save_segment("m1", seg("s1", "Ayşe Yılmaz", "Bütçeyi ben hazırlarım", 0), ayse.id)
    await repo.save_segment(
        "m1", seg("s2", "Ayşe", "Cuma teslim ederim", 3)
    )  # yalnız adla etiketli
    await repo.save_segment("m1", seg("s3", "Deniz Kaya", "Tamam", 6))
    await repo.set_report(
        "m1",
        {
            "summary": "Ayşe Yılmaz bütçeyi hazırlayacak.",
            "action_items": [{"owner": "Ayşe Yılmaz"}],
        },
        {"pdf": "x.pdf"},
    )

    assert await repo.forget_person(ayse.id, requested_by="voice") is True

    meeting = await repo.get_meeting("m1")
    names = [s.speaker_name for s in meeting.segments]
    assert names == [ANONYMOUS_SPEAKER, ANONYMOUS_SPEAKER, "Deniz Kaya"]
    assert all(s.person_id is None for s in meeting.segments)
    assert meeting.report["summary"] == f"{ANONYMOUS_SPEAKER} bütçeyi hazırlayacak."
    assert meeting.report["action_items"][0]["owner"] == ANONYMOUS_SPEAKER
    assert meeting.documents == {}  # eski belgeler yeniden basılmalı

    assert await repo.find_person_by_name("Ayşe") is None
    async with session_scope() as s:
        assert list(await s.scalars(select(Voiceprint))) == []
        assert list(await s.scalars(select(Consent))) == []
        logs = list(await s.scalars(select(AuditLog).where(AuditLog.action == "forget")))
    assert len(logs) == 1
    assert logs[0].detail["segments_anonymized"] == 2
    # Denetim kaydı kişisel veri taşımaz.
    assert "Ayşe" not in str(logs[0].actor) + str(logs[0].detail)


def test_warns_when_sqlite_is_outside_railway_volume(tmp_path, monkeypatch) -> None:
    from katip.store.db import warn_if_not_persistent

    volume = tmp_path / "data"
    volume.mkdir()
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(volume))
    assert warn_if_not_persistent(volume / "katip.db")
    assert not warn_if_not_persistent(tmp_path / "app" / "data" / "katip.db")
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH")
    assert warn_if_not_persistent(tmp_path / "herhangi.db")  # Railway dışında kontrol yok


async def test_usage_is_accumulated_per_meeting_and_reported(database) -> None:
    from types import SimpleNamespace as NS

    from fastapi.testclient import TestClient

    from katip import usage
    from katip.api.main import app

    with usage.meeting("u1"):
        await usage.record_llm(
            "agent",
            "claude-sonnet-5-5",
            NS(
                input_tokens=10_000,
                output_tokens=500,
                cache_read_input_tokens=4_000,
                cache_creation_input_tokens=0,
            ),
        )
    await usage.record_stt("u1", 3600)
    await usage.record_tts("u1", 60)
    # 10k×2 + 500×10 + 4k×0,2 = 25.800 / 1e6 → $0,0258; STT 1 saat $0,12; TTS 1 dk ≈ $0,0117
    client = TestClient(app)
    assert client.get("/api/usage").status_code == 401
    body = client.get("/api/usage", headers={"Authorization": "Bearer test-admin"}).json()
    row = body["meetings"][0]
    assert row["meeting_id"] == "u1" and row["minutes"] == 60.0 and row["llm_calls"] == 1
    assert row["usd"]["claude"] == 0.0258 and row["usd"]["stt"] == 0.12
    assert abs(row["usd"]["tts"] - 0.0117) < 0.0001
    assert body["totals"]["usd"]["total"] == round(0.0258 + 0.12 + 60 / 3600 * 0.70, 4)


async def test_retention_purges_old_meetings_files_and_debug(
    database, tmp_path, monkeypatch
) -> None:
    import os
    import time
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from katip import retention
    from katip.config import get_settings
    from katip.events import Segment

    data = Path(get_settings().data_dir)
    old_pdf = data / "reports" / "eski" / "r.pdf"
    old_pdf.parent.mkdir(parents=True)
    old_pdf.write_bytes(b"%PDF")
    await repo.upsert_meeting("eski", started_at=datetime.now(UTC) - timedelta(days=400))
    await repo.save_segment(
        "eski",
        Segment(
            id="e1", session_id="eski", start_s=0, end_s=1, text="gizli", lang="tr", is_final=True
        ),
    )
    await repo.set_report("eski", {"title": "Eski"}, {"pdf": str(old_pdf)})
    await repo.upsert_meeting("yeni")
    debug = data / "debug"
    debug.mkdir(parents=True)
    (debug / "eski.html").write_text("x")
    os.utime(debug / "eski.html", (time.time() - 9 * 86400,) * 2)
    (debug / "yeni.html").write_text("x")

    result = await retention.purge_once()
    assert result == {"meetings": 1, "files": 1, "debug_files": 1}
    assert await repo.get_meeting("eski") is None and await repo.get_meeting("yeni") is not None
    assert not old_pdf.exists() and not (debug / "eski.html").exists()
    assert (debug / "yeni.html").exists()


def test_retention_text_follows_days(monkeypatch) -> None:
    from katip import privacy
    from katip.config import get_settings

    assert privacy.retention_text() == "12 ay"
    monkeypatch.setenv("KATIP_RETENTION_DAYS", "90")
    get_settings.cache_clear()
    assert privacy.retention_text() == "3 ay"


async def test_forgetting_two_people_from_same_meeting_does_not_collide(database) -> None:
    a = await repo.ensure_person("Deniz Kaya")
    b = await repo.ensure_person("Mehmet Demir")
    await repo.upsert_participant("m9", "Deniz Kaya", a.id, None)
    await repo.upsert_participant("m9", "Mehmet Demir", b.id, None)
    assert await repo.forget_person(b.id, requested_by="test")
    assert await repo.forget_person(a.id, requested_by="test")  # önceden UNIQUE hatası veriyordu
    assert await repo.list_participants("m9") == []
