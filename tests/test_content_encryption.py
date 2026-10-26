"""Toplantı alanları, dosyalar, göç ve anahtar yaşam döngüsü."""

from __future__ import annotations

import base64
import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from docx import Document
from sqlalchemy import event, select, text, update

from katip.api.main import app
from katip.config import get_settings
from katip.documents import service as documents
from katip.events import Segment
from katip.identity import ParticipantIdentity
from katip.reports import service
from katip.reports.summarize import MeetingReport
from katip.store import crypto, db, migration, repo
from katip.store.db import session_scope
from katip.store.models import (
    Meeting,
    MeetingAsset,
    MeetingDataKey,
    MeetingIdentity,
    MeetingMailDraft,
    MeetingNote,
    MeetingParticipant,
    MeetingSegment,
    PreparationDocument,
    TeamsConversation,
    WorkerJob,
)


@pytest.fixture
def encrypted():
    key = os.urandom(32)
    get_settings().data_key = base64.b64encode(key).decode()
    return key


def _row(model):
    shared = {"meeting_id": "m1"}
    if model is Meeting:
        return Meeting(
            id="m1",
            title="Gizli başlık",
            url="https://meet.example/gizli",
            report={"summary": "Gizli özet"},
        )
    values = {
        TeamsConversation: {
            "reference": {
                "conversation_id": "Gizli sohbet",
                "service_url": "https://smba.trafficmanager.net/teams/",
                "tenant": "tenant",
                "bot_id": "bot",
            },
        },
        MeetingMailDraft: {
            "id": "mail1",
            "payload": {"subject": "Gizli özet", "recipients": ["ayse@example.com"]},
        },
        MeetingSegment: {
            "id": "s1",
            "start_s": 1,
            "end_s": 2,
            "lang": "tr",
            "text": "Gizli konuşma",
            "translation": "Private translation",
            "speaker_name": "Ayşe",
            "speaker_email": "ayse@example.com",
        },
        MeetingNote: {"kind": "decision", "text": "Gizli karar", "author": "Ayşe"},
        MeetingParticipant: {"name": "Ayşe", "email": "ayse@example.com", "platform_id": "p1"},
        MeetingIdentity: {"name": "Ayşe", "email": "ayse@example.com", "source": "panel"},
        PreparationDocument: {
            "id": "d1",
            "name": "Gizli.docx",
            "sha256": "a" * 64,
            "pages": [{"text": "Gizli belge", "location": "Sayfa 1"}],
        },
        MeetingAsset: {
            "kind": "screenshot",
            "number": 1,
            "path": "shot.jpg",
            "at_s": 1,
            "caption": "Gizli ekran",
            "requested_by": "Ayşe",
        },
        WorkerJob: {
            "backend": "remote",
            "spec": {"participants": [{"name": "Ayşe"}]},
            "deadline": datetime.now(UTC),
        },
    }
    return model(**shared, **values[model])


@pytest.mark.parametrize("model", crypto.FIELDS)
async def test_fields_round_trip_and_no_plaintext_sql(database, encrypted, model):
    if model is not Meeting:
        await repo.upsert_meeting("m1")
    row = _row(model)
    expected = {f: getattr(row, f) for f in crypto.FIELDS[model]}
    writes = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(("INSERT", "UPDATE")):
            writes.append(str(parameters))

    event.listen(db._engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with session_scope() as session:
            session.add(row)
            await session.flush()
            assert {f: getattr(row, f) for f in expected} == expected
        identity = crypto._identity(row)
        async with session_scope() as session:
            loaded = await session.get(model, identity)
            assert {f: getattr(loaded, f) for f in expected} == expected
            await session.refresh(loaded)
            assert {f: getattr(loaded, f) for f in expected} == expected
            field = next(
                (f for f, v in expected.items() if isinstance(v, str)), next(iter(expected))
            )
            changed = {"name": "Yeni gizli"} if isinstance(expected[field], dict) else "Yeni gizli"
            setattr(loaded, field, changed)
        async with session_scope() as session:
            loaded = await session.get(model, identity)
            assert getattr(loaded, field) == changed
            raw = (await session.execute(select(model.__table__))).first()._mapping
            assert raw["encrypted_content"]
            for name, value in expected.items():
                if value is not None:
                    assert raw[name] != value
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", capture)
    assert "Gizli" not in "".join(writes)
    assert "Ayşe" not in "".join(writes)
    assert "ayse@example.com" not in "".join(writes)


@pytest.mark.parametrize(
    "field", ["text", "translation", "report", "pages", "file:r.pdf", "file:r.docx", "file:001.jpg"]
)
def test_aad_and_key_tampering(field):
    cipher = crypto.MeetingCipher(os.urandom(32), "kurulum", "m1")
    blob = cipher.encrypt(b"icerik", field)
    assert cipher.decrypt(blob, field) == b"icerik"
    for other, name in [
        (cipher, field + "x"),
        (crypto.MeetingCipher(cipher.key, "kurulum", "m2"), field),
        (crypto.MeetingCipher(cipher.key, "baska", "m1"), field),
        (crypto.MeetingCipher(os.urandom(32), "kurulum", "m1"), field),
    ]:
        with pytest.raises(InvalidTag):
            other.decrypt(blob, name)
    with pytest.raises(InvalidTag):
        cipher.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), field)


@pytest.mark.parametrize(
    "extension,data",
    [("pdf", b"%PDF-test"), ("docx", b"PK-test"), ("jpg", b"\xff\xd8test\xff\xd9")],
)
async def test_file_downloads(database, encrypted, tmp_path, extension, data):
    path = tmp_path / ("file." + extension)
    await crypto.write_file("m1", path, data)
    assert path.read_bytes().startswith(crypto.MAGIC)
    assert await crypto.read_file("m1", path) == data
    if extension == "jpg":
        await repo.add_asset("m1", "screenshot", 1, str(path), 1, None)
        url = "/api/meetings/m1/screenshots/1"
    else:
        await repo.set_documents("m1", {extension: str(path)})
        url = "/api/meetings/m1/report." + extension
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(url, headers={"Authorization": "Bearer test-admin"})
        assert response.status_code == 200 and response.content == data
    with pytest.raises(crypto.DataKeyError):
        await crypto.read_file("m2", path)
    get_settings().data_key = ""
    with pytest.raises(crypto.DataKeyError):
        await crypto.read_file("m1", path)
    path.write_bytes(data)
    assert await crypto.read_file("m1", path) == data


async def test_migration_mixed_reads_idempotency_and_rotation(database, tmp_path):
    async with session_scope() as session:
        session.add(_row(Meeting))
    for model in crypto.FIELDS:
        if model is not Meeting:
            async with session_scope() as session:
                session.add(_row(model))
    paths = {ext: tmp_path / ("r." + ext) for ext in ("pdf", "docx", "jpg")}
    for ext, path in paths.items():
        path.write_bytes(ext.encode())
    await repo.set_documents("m1", {k: str(v) for k, v in paths.items() if k != "jpg"})
    async with session_scope() as session:
        asset = await session.get(MeetingAsset, 1)
        asset.path = str(paths["jpg"])
    old = os.urandom(32)
    get_settings().data_key = base64.b64encode(old).decode()
    await repo.add_note("m1", "action", "Yeni aksiyon", "Ayşe")
    assert (await repo.get_meeting("m1")).segments[0].text == "Gizli konuşma"
    assert [n.text for n in await repo.list_notes("m1")] == ["Gizli karar", "Yeni aksiyon"]
    counts = await migration.migrate_content(batch_size=1)
    assert counts == {"rows": len(crypto.FIELDS), "files": 3, "errors": 0}
    snapshots = {ext: path.read_bytes() for ext, path in paths.items()}
    assert await migration.migrate_content(batch_size=1) == {"rows": 0, "files": 0, "errors": 0}
    async with session_scope() as session:
        before = (await session.execute(select(MeetingSegment.encrypted_content))).scalar_one()
        key_before = (await session.get(MeetingDataKey, "m1")).wrapped_key
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()
    with pytest.raises(crypto.DataKeyError):
        await repo.get_meeting("m1")
    get_settings().data_key_previous = base64.b64encode(old).decode()
    assert await migration.rotate_keys(batch_size=1) == 1
    assert await migration.rotate_keys() == 0
    get_settings().data_key_previous = ""
    assert (await repo.get_meeting("m1")).segments[0].text == "Gizli konuşma"
    async with session_scope() as session:
        assert (
            await session.execute(select(MeetingSegment.encrypted_content))
        ).scalar_one() == before
        assert (await session.get(MeetingDataKey, "m1")).wrapped_key != key_before
    for ext, path in paths.items():
        assert path.read_bytes() == snapshots[ext]
        assert await crypto.read_file("m1", path) == ext.encode()


async def test_row_swap_fails(database, encrypted):
    await repo.upsert_meeting("m1")
    async with session_scope() as session:
        session.add(_row(MeetingSegment))
        session.add(
            MeetingSegment(id="s2", meeting_id="m1", start_s=2, end_s=3, text="Başka", lang="tr")
        )
    async with session_scope() as session:
        payload = await session.scalar(
            select(MeetingSegment.encrypted_content).where(MeetingSegment.id == "s1")
        )
        await session.execute(
            update(MeetingSegment)
            .where(MeetingSegment.id == "s2")
            .values(encrypted_content=payload)
        )
    with pytest.raises(InvalidTag):
        await repo.get_meeting("m1")


async def test_crypto_shredding_retention(database, encrypted):
    from katip.retention import purge_once

    path = Path(get_settings().data_dir) / "reports/m1/r.pdf"
    await crypto.write_file("m1", path, b"%PDF-secret")
    blob = path.read_bytes()
    await repo.upsert_meeting(
        "m1", started_at=datetime.now(UTC) - timedelta(days=1000), documents={"pdf": str(path)}
    )
    get_settings().data_key = ""
    assert (await purge_once())["meetings"] == 1
    assert not path.exists()
    async with session_scope() as session:
        assert await session.get(MeetingDataKey, "m1") is None
    get_settings().data_key = base64.b64encode(encrypted).decode()
    with pytest.raises(crypto.DataKeyError):
        await crypto.meeting_cipher("m1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    with pytest.raises(crypto.DataKeyError):
        await crypto.read_file("m1", path)


async def test_require_encryption_startup_fails():
    get_settings().require_encryption = True
    with pytest.raises(crypto.DataKeyError, match="zorunlu"):
        async with app.router.lifespan_context(app):
            pytest.fail("Anahtarsız açılış kabul edildi")
    await db.dispose()


def test_key_file_preferred_and_validation(tmp_path):
    path = tmp_path / "key"
    key = os.urandom(32)
    path.write_text(base64.b64encode(key).decode() + "\n", encoding="utf-8")
    get_settings().data_key = "invalid"
    get_settings().data_key_file = str(path)
    assert crypto.keys()[0] == key
    path.write_text("bad", encoding="utf-8")
    with pytest.raises(crypto.DataKeyError):
        crypto.keys()
    path.write_text(base64.b64encode(b"short").decode(), encoding="utf-8")
    with pytest.raises(crypto.DataKeyError, match="32 bayt"):
        crypto.keys()


async def test_verified_file_failure_keeps_original(database, encrypted, tmp_path, monkeypatch):
    path = tmp_path / "r.pdf"
    path.write_bytes(b"old plaintext")
    cipher = await crypto.meeting_cipher("m1", create=True)

    def refuse(*args):
        raise OSError("Deneme yazma hatası")

    monkeypatch.setattr(crypto.os, "replace", refuse)
    with pytest.raises(OSError):
        cipher.write_file(path, b"old plaintext")
    assert path.read_bytes() == b"old plaintext"
    assert not list(tmp_path.glob(".katip-*"))


async def test_report_and_document_flow(database, encrypted, tmp_path, monkeypatch):
    await repo.upsert_meeting("m1", title="Gizli toplantı")
    for i in range(3):
        await repo.save_segment(
            "m1",
            Segment(
                id=f"s{i}",
                session_id="m1",
                start_s=i,
                end_s=i + 1,
                text=f"Gizli konuşma {i}",
                lang="tr",
                speaker="Ayşe",
            ),
        )
    await repo.upsert_participant("m1", "Ayşe", None, "ayse@example.com")
    await repo.upsert_participant("m1", "Ayşe", None, "ayse@example.com")
    assert len(await repo.list_participants("m1")) == 1
    await repo.save_meeting_identity(
        "m1", ParticipantIdentity(name="Ayşe", email="ayse@example.com"), "panel"
    )
    await repo.save_meeting_identity(
        "m1", ParticipantIdentity(name="Ayşe", email="ayse@example.com"), "panel"
    )
    assert len(await repo.list_meeting_identities("m1")) == 1
    await documents.save_document(
        "m1", "Gizli belge", b"original", [{"text": "Gizli belge içeriği", "location": "Sayfa 1"}]
    )
    assert "Gizli belge içeriği" in await documents.document_context("m1")
    await repo.add_note("m1", "decision", "Gizli karar", "Ayşe")

    class Page:
        async def set_content(self, html, **kwargs):
            assert "Gizli özet" in html

        async def pdf(self, **kwargs):
            assert "path" not in kwargs
            return b"%PDF-fake"

    class Browser:
        async def new_page(self):
            return Page()

        async def close(self):
            pass

    class Playwright:
        async def __aenter__(self):
            async def launch(**kwargs):
                return Browser()

            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("playwright.async_api.async_playwright", Playwright)

    async def summarize(segments, **kwargs):
        assert segments[0]["text"] == "Gizli konuşma 0"
        assert "Gizli karar" in kwargs["notes"] and "ayse@example.com" in kwargs["org_context"]
        return MeetingReport(
            title="Özet",
            summary="Gizli özet",
            key_points=[],
            decisions=[],
            action_items=[],
            open_questions=[],
            participants=[],
        )

    paths = await service.generate_report("m1", summarizer=summarize)
    for path in paths.values():
        assert Path(path).read_bytes().startswith(crypto.MAGIC)
    doc = Document(io.BytesIO(await crypto.read_file("m1", paths["docx"])))
    assert "Gizli konuşma 0" in "\n".join(p.text for p in doc.paragraphs)
    assert await crypto.read_file("m1", paths["pdf"]) == b"%PDF-fake"
    assert (await repo.recent_reports())[0].report["summary"] == "Gizli özet"


async def test_legacy_schema_and_missing_report(database):
    await repo.upsert_meeting("m1", title="Eski toplantı")
    for i in range(3):
        await repo.save_segment(
            "m1",
            Segment(id=f"s{i}", session_id="m1", start_s=i, end_s=i + 1, text="Konuşma", lang="tr"),
        )
    async with db._engine.begin() as connection:
        await connection.execute(text("ALTER TABLE meetings DROP COLUMN encrypted_content"))
        await connection.execute(text("ALTER TABLE meeting_segments DROP COLUMN encrypted_content"))
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()
    await db.init_db()
    assert (await repo.get_meeting("m1")).title == "Eski toplantı"
    await migration.migrate_content(batch_size=1)
    assert await repo.meetings_missing_report() == ["m1"]
    assert await repo.recent_reports() == []


async def test_admin_and_preparation_deletion(database, encrypted):
    for meeting_id in ("finished", "prepared"):
        path = Path(get_settings().data_dir) / "screenshots" / meeting_id / "001.jpg"
        await crypto.write_file(meeting_id, path, b"screenshot")
        await repo.upsert_meeting(
            meeting_id, state="prepared" if meeting_id == "prepared" else "ended"
        )
        await repo.add_asset(meeting_id, "screenshot", 1, str(path), 1, "Ayşe")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for url in ("/api/meetings/finished/content", "/api/preparations/prepared"):
            response = await client.delete(url, headers={"Authorization": "Bearer test-admin"})
            assert response.status_code == 200
    async with session_scope() as session:
        assert not list(await session.scalars(select(MeetingDataKey)))
    assert not list((Path(get_settings().data_dir) / "screenshots").glob("*/*.jpg"))


async def test_forget_and_identity_binding_remain_encrypted(database, encrypted):
    person = await repo.ensure_person_by_email("ayse@example.com", "Ayşe")
    await repo.upsert_meeting("m1")
    async with session_scope() as session:
        session.add(_row(MeetingSegment))
    identity = ParticipantIdentity(name="Ayşe", email="ayse@example.com")
    await repo.bind_participant("m1", "Ayşe", identity, "panel")
    assert (await repo.get_meeting("m1")).segments[0].speaker_email == identity.email
    await repo.set_segment_person(["s1"], person.id, person.display_name)
    await repo.save_meeting_identity("m1", identity, "panel")
    path = Path(get_settings().data_dir) / "reports/m1/r.pdf"
    await crypto.write_file("m1", path, b"%PDF-Ayse")
    await repo.set_report("m1", {"summary": "Ayşe kararı"}, {"pdf": str(path)})
    assert await repo.forget_person(person.id, requested_by="test")
    meeting = await repo.get_meeting("m1")
    assert meeting.segments[0].speaker_name == "Anonim katılımcı"
    assert meeting.segments[0].speaker_email is None
    assert meeting.report == {"summary": "Anonim katılımcı kararı"}
    assert not path.exists()
    assert await repo.list_participants("m1") == []
    assert await repo.list_meeting_identities("m1") == []
    async with session_scope() as session:
        assert await session.get(MeetingDataKey, "m1") is not None
        raw = (await session.execute(select(MeetingSegment.__table__))).first()._mapping
        assert raw["speaker_name"].startswith("km2:") and raw["encrypted_content"]


async def test_agent_context_reads_encrypted_evidence(database, encrypted):
    from katip.agent.controller import MeetingAgent

    await repo.upsert_meeting("m1")
    await documents.save_document(
        "m1", "Belge", b"data", [{"text": "Gizli kaynak", "location": "Sayfa 1"}]
    )
    await repo.set_report("m1", {"title": "Başlık", "summary": "Gizli özet"}, {})
    account = await repo.set_panel_account("ayse@example.com", "fake", role="admin")
    host = SimpleNamespace(
        session_id="m1",
        participants_all_internal=lambda: True,
        verified_internal_emails=lambda: {"ayse@example.com"},
        transcript_text=lambda last: "Ayşe: Gizli konuşma",
        reply_mode="chat",
    )
    agent = MeetingAgent(host)
    context = await agent._turn_context(request="Gizli kaynak")
    assert "Gizli konuşma" in context.transcript
    assert "Gizli kaynak" in context.documents
    host.session_id = "m2"
    await repo.upsert_meeting("m2", owner_account_id=account.id)
    assert "Gizli özet" in " ".join(await agent._past_meetings())


async def test_screenshot_session_writes_encrypted_file(database, encrypted):
    import asyncio

    from katip.pipeline.session import MeetingSession
    from katip.pipeline.stt.base import STTProvider

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, **kwargs):
            if False:
                yield

    session = MeetingSession("m1", stt=NoSTT(), voice=False)
    session.request_screenshot("Ayşe")
    request = session.outbox.get_nowait()
    jpeg = b"\xff\xd8fake screenshot"
    assert await session.on_screenshot(request["request_id"], jpeg) == 1
    await asyncio.sleep(0)
    assets = await repo.list_assets("m1")
    assert assets[0].requested_by == "Ayşe"
    assert Path(assets[0].path).read_bytes().startswith(crypto.MAGIC)
    assert await crypto.read_file("m1", assets[0].path) == jpeg


async def test_migration_verification_failure_preserves_plaintext(database, tmp_path, monkeypatch):
    await repo.upsert_meeting("m1", title="Eski içerik")
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(crypto.MeetingCipher, "decrypt", lambda *args: b"bozuk")
    assert (await migration.migrate_content())["errors"] == 1
    async with session_scope() as session:
        raw = (await session.execute(select(Meeting.__table__))).first()._mapping
        assert raw["title"] == "Eski içerik" and raw["encrypted_content"] is None
        assert await session.get(MeetingDataKey, "m1") is None
    path = tmp_path / "r.pdf"
    path.write_bytes(b"old plaintext")
    cipher = crypto.MeetingCipher(os.urandom(32), "installation", "m1")
    with pytest.raises(crypto.DataKeyError, match="doğrulanamadı"):
        cipher.write_file(path, b"old plaintext")
    assert path.read_bytes() == b"old plaintext"
    assert not list(tmp_path.glob(".katip-*"))


async def test_wrapped_key_aad_rejects_another_meeting(database, encrypted):
    await repo.upsert_meeting("m1", title="Birinci")
    await repo.upsert_meeting("m2", title="İkinci")
    async with session_scope() as session:
        wrapped = (await session.get(MeetingDataKey, "m1")).wrapped_key
        await session.execute(
            update(MeetingDataKey)
            .where(MeetingDataKey.meeting_id == "m2")
            .values(wrapped_key=wrapped)
        )
    with pytest.raises(InvalidTag):
        await repo.get_meeting("m2")


async def test_plaintext_mode_health_warning(database, monkeypatch):
    from katip.api.routes import admin

    async def fake_check():
        return True, "Deneme"

    monkeypatch.setattr(admin, "_check_soniox", fake_check)
    monkeypatch.setattr(admin, "_check_anthropic", fake_check)
    checks = await admin.health_checks()
    encryption = next(c for c in checks if c["key"] == "data_encryption")
    assert encryption["ok"] is None and "düz metin" in encryption["detail"]


async def test_startup_logs_one_plaintext_warning(monkeypatch):
    from katip.api import main

    warnings = []
    monkeypatch.setattr(main.log, "warning", lambda message, **kwargs: warnings.append(message))
    async with app.router.lifespan_context(app):
        pass
    assert sum("düz metin" in message for message in warnings) == 1


async def test_worker_claim_reads_encrypted_spec_after_bulk_refresh(database, encrypted):
    from katip.workers import jobs

    get_settings().bot_backend = "remote"
    get_settings().worker_manager_keys = {"manager": "fake-" * 8}
    await repo.upsert_meeting("m1")
    await jobs.enqueue("m1", {"max_minutes": 1, "url": "https://meet.example/gizli"}, 1)
    job, _token = await jobs.claim("manager")
    assert job.spec["url"] == "https://meet.example/gizli"
    assert job.status == "running"
    async with session_scope() as session:
        raw = (await session.execute(select(WorkerJob.__table__))).first()._mapping
        assert raw["spec"] == {} and raw["encrypted_content"]


async def test_renderer_cannot_write_plaintext_when_key_is_configured(encrypted, tmp_path):
    from katip.reports.render import ReportContext, render_docx, render_pdf

    report = MeetingReport(
        title="Gizli",
        summary="Gizli",
        key_points=[],
        decisions=[],
        action_items=[],
        open_questions=[],
        participants=[],
    )
    ctx = ReportContext(report=report, meeting_id="m1")
    with pytest.raises(crypto.DataKeyError):
        render_docx(ctx, tmp_path / "r.docx")
    with pytest.raises(crypto.DataKeyError):
        await render_pdf(ctx, tmp_path / "r.pdf")
    assert not (tmp_path / "r.docx").exists() and not (tmp_path / "r.pdf").exists()
