"""Şifreleme yeniden incelemesi: batch, KVKK, commit ve SQLite eşzamanlılığı."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from katip import retention
from katip.api.main import app
from katip.api.routes import admin
from katip.config import get_settings
from katip.events import Segment
from katip.reports import service
from katip.reports.summarize import MeetingReport
from katip.store import crypto, db, migration, repo
from katip.store.db import session_scope, write_scope
from katip.store.models import AuditLog, EncryptionInstallation, MeetingSegment, MeetingTombstone


@pytest.fixture
def encrypted():
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()


async def seed_report():
    for i in range(3):
        await repo.save_segment(
            "m1",
            Segment(id=f"s{i}", session_id="m1", start_s=i, end_s=i + 1, text="Söz", lang="tr"),
        )
    report = MeetingReport(
        title="Ara rapor",
        summary="Özet",
        key_points=[],
        decisions=[],
        action_items=[],
        open_questions=[],
        participants=[],
    )
    await repo.set_report("m1", report.model_dump(), {"pdf": "interim.pdf"})
    return report


@pytest.mark.parametrize("has_batch_id", [False, True])
async def test_failed_final_batch_clears_markers_and_keeps_interim(
    database, encrypted, has_batch_id
):
    report = await seed_report()

    async def fail(*args, on_batch, **kwargs):
        if has_batch_id:
            await on_batch("failed-batch")
        assert service.batch_is_pending((await repo.get_meeting("m1")).documents)
        raise RuntimeError("Rapor başarısız")

    with pytest.raises(RuntimeError, match="Rapor başarısız"):
        await service.generate_report("m1", summarizer=fail, batch=True, resummarize=True)
    meeting = await repo.get_meeting("m1")
    assert meeting.report == report.model_dump()
    assert meeting.documents == {"pdf": "interim.pdf"}
    assert await repo.delete_meeting("m1")


@pytest.mark.parametrize("age", [None, "stale", "fresh"])
async def test_retention_expires_batch_markers(database, encrypted, age):
    settings = get_settings()
    await repo.upsert_meeting("m1", started_at=datetime.now(UTC) - timedelta(days=500))
    documents = {"batch": "old-batch"}
    if age is not None:
        wait = settings.report_batch_max_wait_s + settings.report_deadline_s + 60
        elapsed = wait + 1 if age == "stale" else wait - 30
        documents["batch_started_at"] = (datetime.now(UTC) - timedelta(seconds=elapsed)).isoformat()
    await repo.set_documents("m1", documents)
    ids, _ = await repo.purge_meetings_before(datetime.now(UTC))
    assert ids == ([] if age == "fresh" else ["m1"])
    assert (await repo.get_meeting("m1") is not None) == (age == "fresh")


async def test_admin_cancels_persisted_batch_before_delete(database, encrypted, monkeypatch):
    await repo.upsert_meeting("m1")
    await repo.set_documents(
        "m1", {"batch": "persisted-batch", "batch_started_at": datetime.now(UTC).isoformat()}
    )
    cancelled = AsyncMock()
    provider = SimpleNamespace(
        client=SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(cancel=cancelled)))
    )
    registry = SimpleNamespace(llm_provider=lambda name: provider)
    monkeypatch.setattr("katip.providers.registry.get_registry", lambda: registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            "/api/meetings/m1/content", headers={"Authorization": "Bearer test-admin"}
        )
    assert response.status_code == 200
    cancelled.assert_awaited_once_with("persisted-batch")
    assert await repo.get_meeting("m1") is None


async def test_batch_cancel_failure_keeps_retry_id(database, encrypted, monkeypatch):
    await repo.upsert_meeting("m1")
    documents = {"batch": "retry-batch", "batch_started_at": datetime.now(UTC).isoformat()}
    await repo.set_documents("m1", documents)
    monkeypatch.setattr(service, "cancel_batch", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await repo.delete_meeting("m1", cancel_pending=True)
    assert (await repo.get_meeting("m1")).documents == documents


async def test_resumed_batch_keeps_id_until_cancelled(database, encrypted, monkeypatch):
    await seed_report()
    await repo.update_batch_marker(
        "m1", {"batch": "resume-batch", "batch_started_at": datetime.now(UTC).isoformat()}
    )
    entered = asyncio.Event()

    async def resume(*args, resume_batch, **kwargs):
        assert resume_batch == "resume-batch"
        assert (await repo.get_meeting("m1")).documents["batch"] == resume_batch
        entered.set()
        await asyncio.Event().wait()

    cancel = AsyncMock()
    monkeypatch.setattr(service, "cancel_batch", cancel)
    task = asyncio.create_task(
        service.generate_report("m1", summarizer=resume, batch=True, resummarize=True)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await repo.delete_meeting("m1", cancel_pending=True)
        cancel.assert_awaited_once_with("resume-batch")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_partial_erasure_audit_health_and_scheduled_retry(database, encrypted, monkeypatch):
    person = await repo.ensure_person_by_email("ayse@example.com", "Ayşe")
    await repo.set_report("m1", {"summary": "Ayşe ayse@example.com"}, {})
    erase = repo._erase_meeting_person
    monkeypatch.setattr(repo, "_erase_meeting_person", AsyncMock(side_effect=crypto.DataKeyError))
    assert not await repo.forget_person(person.id, requested_by="voice")
    pending = await repo.get_person(person.id)
    assert pending.erasure_pending and pending.email == person.email
    assert await repo.pending_erasure_ids() == [person.id]
    async with session_scope() as s:
        audit = await s.scalar(select(AuditLog).where(AuditLog.action == "forget"))
        assert len(audit.detail["failed_meetings"]) == 1
        assert person.email not in str(audit.detail)
    for name in ("_check_soniox", "_check_anthropic", "_check_db"):
        monkeypatch.setattr(admin, name, AsyncMock(return_value=(True, "Test")))
    check = next(c for c in await admin.health_checks() if c["key"] == "pending_erasure")
    assert check["ok"] is False and "1 talep" in check["detail"]
    monkeypatch.setattr(repo, "_erase_meeting_person", erase)
    completed = asyncio.Event()
    forget = repo.forget_person

    async def retry(person_id, *, requested_by):
        assert requested_by == "retry"
        result = await forget(person_id, requested_by=requested_by)
        completed.set()
        return result

    monkeypatch.setattr(repo, "forget_person", retry)
    task = asyncio.create_task(repo.retry_erasures())
    try:
        await asyncio.wait_for(completed.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await repo.pending_erasure_ids() == []
    assert await repo.get_person(person.id) is None
    assert (await repo.get_meeting("m1")).report == {"summary": "Anonim katılımcı Anonim katılımcı"}


async def test_partial_erasure_admin_api_reports_pending(database, monkeypatch):
    person = await repo.ensure_person("Ayşe")
    await repo.set_report("m1", {"summary": "Ayşe"}, {})
    monkeypatch.setattr(repo, "_erase_meeting_person", AsyncMock(side_effect=RuntimeError))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            f"/api/people/{person.id}", headers={"Authorization": "Bearer test-admin"}
        )
    assert response.status_code == 200
    assert response.json()["forgotten"] is False
    assert response.json()["pending"] is True
    assert len(response.json()["failed_meetings"]) == 1


async def test_erasure_file_failure_keeps_person_and_paths_for_retry(
    database, encrypted, tmp_path, monkeypatch
):
    person = await repo.ensure_person("Ayşe")
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes("Ayşe".encode())
    await repo.set_report("m1", {"summary": "Ayşe"}, {"pdf": str(pdf)})
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == pdf:
            raise PermissionError("Dosya meşgul")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    assert not await repo.forget_person(person.id, requested_by="test")
    assert (await repo.get_meeting("m1")).documents == {"pdf": str(pdf)}
    assert (await repo.get_person(person.id)).erasure_pending
    monkeypatch.setattr(Path, "unlink", unlink)
    assert await repo.forget_person(person.id, requested_by="retry")
    assert not pdf.exists()


async def test_delete_commit_failure_does_not_mark_files_dead(database, encrypted, tmp_path):
    cipher = await crypto.meeting_cipher("m1", create=True)

    def fail(session):
        if session.connection().scalar(select(MeetingTombstone.meeting_id)):
            raise RuntimeError("Commit başarısız")

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(RuntimeError, match="Commit başarısız"):
            await repo.delete_meeting("m1")
    finally:
        event.remove(Session, "before_commit", fail)
    assert await repo.get_meeting("m1") is not None
    cipher.write_file(tmp_path / "live.pdf", "Hâlâ yazılabilir".encode())
    assert cipher.read_file(tmp_path / "live.pdf") == "Hâlâ yazılabilir".encode()
    assert (await migration.migrate_content())["errors"] == 0
    async with session_scope() as s:
        assert await s.scalar(select(EncryptionInstallation.migration_completed))
        assert await s.get(MeetingTombstone, "m1") is None


@pytest.mark.parametrize("separate_marker", [False, True])
async def test_migration_repairs_orphan_deletion_marker(
    database, encrypted, tmp_path, separate_marker
):
    await repo.upsert_meeting("m1")
    path = tmp_path / "old.pdf"
    path.write_bytes(b"Eski rapor")
    await repo.set_documents("m1", {"pdf": str(path)})
    with crypto.file_lock("m1", deleting=True) as stream:
        if separate_marker:
            crypto.mark_deleted("m1", stream)
        else:
            stream.seek(0)
            stream.truncate(0)
            stream.write(b"1")
            stream.flush()
    assert (await migration.migrate_content())["errors"] == 0
    assert await crypto.read_file("m1", path) == b"Eski rapor"
    await crypto.write_file("m1", path, b"Yeni rapor")


async def test_sqlite_reads_continue_during_encrypted_write(database, encrypted):
    await seed_report()
    statements = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement)

    async with write_scope() as writer:
        row = await writer.get(MeetingSegment, "s0")
        row.translation = "Uncommitted"
        await writer.flush()
        event.listen(db._engine.sync_engine, "before_cursor_execute", capture)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    repo.get_meeting("m1"), repo.list_notes("m1"), repo.list_assets("m1")
                ),
                2,
            )
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", capture)
        assert results[0].segments[0].translation is None
        assert not any("BEGIN IMMEDIATE" in statement for statement in statements)
    assert (await repo.get_meeting("m1")).segments[0].translation == "Uncommitted"
    async with session_scope() as s:
        assert await s.scalar(text("PRAGMA journal_mode")) == "wal"
        assert await s.scalar(text("PRAGMA busy_timeout")) == 30000


async def test_encrypted_writers_preserve_translation_under_contention(database, encrypted):
    await seed_report()
    person = await repo.ensure_person("Ayşe")
    task = None
    async with write_scope() as writer:
        row = await writer.get(MeetingSegment, "s0")
        row.translation = "New translation"
        await writer.flush()
        task = asyncio.create_task(repo.set_segment_person(["s0"], person.id, "Ayşe"))
        await asyncio.sleep(0.05)
        assert not task.done()
    await asyncio.wait_for(task, 5)
    saved = (await repo.get_meeting("m1")).segments[0]
    assert saved.translation == "New translation" and saved.speaker_name == "Ayşe"


async def test_deletion_cleans_lock_and_old_cipher_stays_dead(database, encrypted, tmp_path):
    cipher = await crypto.meeting_cipher("m1", create=True)
    cipher.write_file(tmp_path / "old.pdf", "İçerik".encode())
    locks = Path(get_settings().data_dir) / ".locks"
    assert list(locks.glob("*.lock"))
    assert await repo.delete_meeting("m1")
    assert not list(locks.glob("*.lock"))
    with pytest.raises(crypto.DataKeyError, match="Silinen"):
        cipher.write_file(tmp_path / "late.pdf", "Geç yazma".encode())
    assert not list(locks.glob("*.lock"))


async def test_periodic_lock_cleanup_skips_active_holder_and_live_meeting(database, encrypted):
    await repo.upsert_meeting("live")
    with crypto.file_lock("live"):
        pass
    locks = Path(get_settings().data_dir) / ".locks"
    with crypto.file_lock("orphan"):
        await retention.purge_once()
        assert (locks / crypto.lock_name("orphan")).exists()
    await retention.purge_once()
    assert not (locks / crypto.lock_name("orphan")).exists()
    assert (locks / crypto.lock_name("live")).exists()


async def test_lock_cleanup_does_not_split_waiter_from_holder(database):
    holder = crypto.file_lock("orphan")
    await asyncio.to_thread(holder.__enter__)

    def wait_for_lock():
        with crypto.file_lock("orphan"):
            return True

    waiter = asyncio.create_task(asyncio.to_thread(wait_for_lock))
    try:
        await asyncio.sleep(0.05)
        assert await asyncio.to_thread(crypto.cleanup_orphan_locks, set()) == 0
        assert not waiter.done()
    finally:
        await asyncio.to_thread(holder.__exit__, None, None, None)
    assert await asyncio.wait_for(waiter, 5)
    assert await asyncio.to_thread(crypto.cleanup_orphan_locks, set()) == 1


async def test_cleanup_preserves_legacy_deletion_marker(database, encrypted, tmp_path):
    cipher = await crypto.meeting_cipher("m1", create=True)
    assert await repo.delete_meeting("m1")
    crypto._deleted_path("m1").unlink()
    with crypto.file_lock("m1", deleting=True) as stream:
        stream.seek(0)
        stream.truncate(0)
        stream.write(b"1")
        stream.flush()
    await retention.purge_once()
    locks = Path(get_settings().data_dir) / ".locks"
    assert not list(locks.glob("*.lock"))
    with pytest.raises(crypto.DataKeyError, match="Silinen"):
        cipher.write_file(tmp_path / "late.pdf", b"late")
