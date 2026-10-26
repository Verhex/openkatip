"""Şifreleme güvenlik incelemesindeki dokuz bulgu için regresyonlar."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, select, update

from katip.api.main import app
from katip.config import get_settings
from katip.documents import service as documents
from katip.events import Segment
from katip.identity import ParticipantIdentity
from katip.reports import service
from katip.store import crypto, db, migration, repo
from katip.store.db import session_scope
from katip.store.models import (
    AuditLog,
    EncryptionInstallation,
    Meeting,
    MeetingDataKey,
    MeetingParticipant,
    MeetingSegment,
    MeetingTombstone,
)


@pytest.fixture
def encrypted():
    key = os.urandom(32)
    get_settings().data_key = base64.b64encode(key).decode()
    return key


def segment(identity="s1", **kwargs):
    return Segment(
        id=identity, session_id="m1", start_s=0, end_s=1, text="Konuşma", lang="tr", **kwargs
    )


@pytest.mark.parametrize("use_key", [False, True])
async def test_deleted_id_refuses_all_late_writes(database, use_key, tmp_path):
    if use_key:
        get_settings().data_key = base64.b64encode(os.urandom(32)).decode()
    cipher = await crypto.meeting_cipher("m1", create=True)
    assert await repo.delete_meeting("m1")
    writes = [
        repo.upsert_meeting("m1", title="Geç başlık"),
        repo.save_segment("m1", segment()),
        repo.add_usage("m1", llm_calls=1),
        repo.add_asset("m1", "screenshot", 1, "late.jpg", 0, None),
        crypto.write_file("m1", tmp_path / "late.jpg", b"late"),
        crypto.meeting_cipher("m1", create=True),
    ]
    for write in writes:
        with pytest.raises(crypto.DataKeyError, match="Silinen"):
            await write
    if cipher:
        with pytest.raises(crypto.DataKeyError, match="Silinen"):
            cipher.write_file(tmp_path / "late.docx", b"transcript")
        async with session_scope() as s:
            with pytest.raises(crypto.DataKeyError, match="Silinen"):
                await s.run_sync(
                    lambda sync: crypto.cipher_for(sync.connection(), "m1", create=True)
                )
    async with session_scope() as s:
        assert await s.get(MeetingTombstone, "m1")
        assert await s.get(Meeting, "m1") is None
        assert await s.get(MeetingDataKey, "m1") is None
    assert not list(tmp_path.glob("late.*"))


async def test_admin_batch_deletion_cancels_batch_and_poller(database, encrypted, monkeypatch):
    for i in range(3):
        await repo.save_segment("m1", segment(f"s{i}"))
    entered = asyncio.Event()
    cancelled = []

    async def cancel(batch_id):
        assert await repo.get_meeting("m1") is not None
        cancelled.append(batch_id)

    monkeypatch.setattr(service, "cancel_batch", cancel)

    async def poller(*args, on_batch, **kwargs):
        await on_batch("batch-test")
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(service.generate_report("m1", summarizer=poller, batch=True))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.delete(
                "/api/meetings/m1/content", headers={"Authorization": "Bearer test-admin"}
            )
        assert response.status_code == 200
        assert task.done()
        assert cancelled == ["batch-test"]
        assert await repo.get_meeting("m1") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_deletion_cancels_and_awaits_regular_report(database, encrypted):
    for i in range(3):
        await repo.save_segment("m1", segment(f"s{i}"))
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def summarize(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(service.generate_report("m1", summarizer=summarize))
    await asyncio.wait_for(entered.wait(), 5)
    assert await repo.delete_meeting("m1")
    assert stopped.is_set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert not list((Path(get_settings().data_dir) / "reports").glob("*/*"))


async def test_concurrent_stale_speaker_update_preserves_translation(database, encrypted):
    person = await repo.ensure_person_by_email("a@example.com", "Ayşe")
    await repo.save_segment("m1", segment(speaker="İlk ad"))
    async with db._sessionmaker() as stale:
        row = await stale.get(MeetingSegment, "s1")
        await stale.commit()  # İkinci yazıcı ilk kopyayı yükledikten sonra ilerler.
        await repo.save_segment("m1", segment(speaker="İlk ad", translation="New translation"))
        row.person_id, row.speaker_name = person.id, "Ayşe"
        await stale.commit()
    saved = (await repo.get_meeting("m1")).segments[0]
    assert saved.translation == "New translation" and saved.speaker_name == "Ayşe"


async def test_metadata_flush_does_not_reencrypt_stale_report(database, encrypted):
    await repo.set_report("m1", {"summary": "İlk"}, {})
    async with db._sessionmaker() as stale:
        row = await stale.get(Meeting, "m1")
        await stale.commit()
        await repo.set_report("m1", {"summary": "Yeni"}, {})
        async with session_scope() as s:
            before = await s.scalar(select(Meeting.encrypted_content))
        row.documents = {"docx": "new.docx"}
        await stale.commit()
    async with session_scope() as s:
        assert await s.scalar(select(Meeting.encrypted_content)) == before
    assert (await repo.get_meeting("m1")).report == {"summary": "Yeni"}


@pytest.mark.parametrize("mode", ["missing", "wrong", "previous"])
async def test_startup_validates_stored_key_ids(database, encrypted, mode):
    await repo.upsert_meeting("m1")
    get_settings().data_key = "" if mode == "missing" else base64.b64encode(os.urandom(32)).decode()
    if mode == "previous":
        get_settings().data_key_previous = base64.b64encode(encrypted).decode()
        await db.init_db()
    else:
        with pytest.raises(crypto.DataKeyError, match="Şifreli toplantılar"):
            await db.init_db()


async def test_encryption_cannot_revert_to_plaintext_even_after_last_deletion(database, encrypted):
    await repo.upsert_meeting("m1")
    assert await repo.delete_meeting("m1")
    get_settings().data_key = ""
    with pytest.raises(crypto.DataKeyError, match="yeni içerik"):
        await repo.save_segment("m2", segment())
    async with session_scope() as s:
        assert await s.get(Meeting, "m2") is None


async def test_migration_item_failure_deleted_item_and_retry(database, monkeypatch):
    for mid in ("bad", "deleted", "good"):
        await repo.upsert_meeting(mid, title="Eski içerik")
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()
    original = migration._migrate_row

    async def migrate(model, identity):
        if model is Meeting and identity == "bad":
            raise ValueError("Loga yazılmaması gereken içerik")
        if model is Meeting and identity == "deleted":
            await repo.delete_meeting(identity)
        return await original(model, identity)

    monkeypatch.setattr(migration, "_migrate_row", migrate)
    counts = await migration.migrate_content(batch_size=3)
    assert counts["errors"] == 1 and counts["rows"] == 1
    assert await repo.get_meeting("deleted") is None
    assert (await repo.get_meeting("good")).title == "Eski içerik"
    monkeypatch.setattr(migration, "_migrate_row", original)
    assert (await migration.migrate_content())["errors"] == 0
    assert (await repo.get_meeting("bad")).title == "Eski içerik"


async def test_background_migration_retries_on_schedule(database, encrypted, monkeypatch):
    calls = 0
    retried = asyncio.Event()

    async def migrate():
        nonlocal calls
        calls += 1
        if calls >= 2:
            retried.set()
        return {"rows": 0, "files": 0, "errors": 1 if calls == 1 else 0}

    monkeypatch.setattr(migration, "migrate_content", migrate)
    monkeypatch.setattr(migration, "_RETRY_S", 0.01)
    task = asyncio.create_task(migration.run_background())
    try:
        await asyncio.wait_for(retried.wait(), 5)
        assert migration.state == "tamamlandı"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rotation_bad_item_does_not_rollback_other_keys(database, encrypted):
    for mid in ("bad", "good"):
        await repo.upsert_meeting(mid, title="İçerik")
    async with session_scope() as s:
        await s.execute(
            update(MeetingDataKey)
            .where(MeetingDataKey.meeting_id == "bad")
            .values(key_id="unknown")
        )
    get_settings().data_key_previous = base64.b64encode(encrypted).decode()
    new_key = os.urandom(32)
    get_settings().data_key = base64.b64encode(new_key).decode()
    with pytest.raises(crypto.DataKeyError, match="önceki anahtarı koruyun"):
        await migration.rotate_keys(batch_size=2)
    async with session_scope() as s:
        assert (await s.get(MeetingDataKey, "good")).key_id == crypto.key_id(new_key)
    get_settings().data_key_previous = ""
    assert (await repo.get_meeting("good")).title == "İçerik"


async def test_file_migration_and_renderer_share_cross_process_lock(database, encrypted, tmp_path):
    path = tmp_path / "r.docx"
    path.write_bytes(b"old")
    cipher = await crypto.meeting_cipher("m1", create=True)
    ready, release = tmp_path / "ready", tmp_path / "release"
    code = """
import sys, time
from pathlib import Path
from katip.store import crypto
crypto.get_settings().data_dir = sys.argv[1]
cipher = crypto.MeetingCipher(bytes.fromhex(sys.argv[2]), sys.argv[3], 'm1')
original = crypto.atomic_write
def paused(*args):
    Path(sys.argv[5]).touch()
    deadline = time.monotonic() + 10
    while not Path(sys.argv[6]).exists():
        if time.monotonic() > deadline:
            raise TimeoutError('test timeout')
        time.sleep(0.01)
    return original(*args)
crypto.atomic_write = paused
cipher.migrate_file(Path(sys.argv[4]))
"""
    process = await asyncio.to_thread(
        subprocess.Popen,
        [
            sys.executable,
            "-c",
            code,
            get_settings().data_dir,
            cipher.key.hex(),
            cipher.installation_id,
            str(path),
            str(ready),
            str(release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    writer = None
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                assert process.poll() is None
                await asyncio.sleep(0.01)
        writer = asyncio.create_task(asyncio.to_thread(cipher.write_file, path, b"new"))
        await asyncio.sleep(0.1)
        assert not writer.done()
        release.touch()
        await asyncio.wait_for(writer, 5)
        await asyncio.to_thread(process.communicate, timeout=5)
        assert process.returncode == 0
        assert cipher.read_file(path) == b"new"
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
        await asyncio.to_thread(process.communicate)
        if writer:
            await asyncio.gather(writer, return_exceptions=True)


async def test_deletion_removes_all_debug_artifacts(database, encrypted):
    await repo.upsert_meeting("m1")
    debug = Path(get_settings().data_dir) / "debug"
    debug.mkdir(parents=True)
    for name in ("m1.json", "m1.png", "m1.html", "m1-frame.html", "m1_extra.txt", "m10.png"):
        (debug / name).write_bytes(b"debug")
    (debug / "m1").mkdir()
    (debug / "m1" / "trace.txt").write_bytes(b"trace")
    await repo.delete_meeting("m1")
    assert [p.name for p in debug.iterdir()] == ["m10.png"]


async def test_v1_reads_and_lookup_migration_preserve_dedup(database, encrypted):
    await repo.upsert_participant("m1", "Ayşe", None, "a@example.com")
    cipher = await crypto.meeting_cipher("m1")
    assert cipher.subkey("katip:content:v2") != cipher.subkey("katip:lookup:v2") != cipher.key
    async with session_scope() as s:
        row = await s.scalar(select(MeetingParticipant))
        values = {f: getattr(row, f) for f in crypto.FIELDS[MeetingParticipant]}
        legacy = {
            f: base64.b64encode(
                crypto.seal(
                    cipher.key,
                    json.dumps(v).encode(),
                    cipher.aad(f"meeting_participants:{row.id}:{f}"),
                )
            ).decode()
            for f, v in values.items()
        }
        await s.execute(
            MeetingParticipant.__table__.update().values(
                name=cipher.lookup(MeetingParticipant, "name", "Ayşe", version=1),
                encrypted_content=legacy,
            )
        )
    assert (await repo.list_participants("m1"))[0].name == "Ayşe"
    await repo.upsert_participant("m1", "Ayşe", None, "a@example.com")
    assert len(await repo.list_participants("m1")) == 1
    assert (await migration.migrate_content())["errors"] == 0
    async with session_scope() as s:
        assert (await s.scalar(select(MeetingParticipant.name))).startswith("km2:")
    assert (await repo.list_participants("m1"))[0].name == "Ayşe"
    old = crypto.seal(cipher.key, b"v1", cipher.aad("file:r.pdf"))
    assert cipher.decrypt(old, "file:r.pdf") == b"v1"
    assert cipher.encrypt(b"v2", "text").startswith(crypto.MAGIC)


async def test_document_hash_is_meeting_hmac_with_dedup_and_legacy_migration(database, encrypted):
    for mid in ("m1", "m2"):
        await repo.upsert_meeting(mid)
    data, pages = b"same document", [{"text": "İçerik", "location": "Sayfa 1"}]
    first = await documents.save_document("m1", "Belge", data, pages)
    repeat = await documents.save_document("m1", "Belge", data, pages)
    other = await documents.save_document("m2", "Belge", data, pages)
    assert first.id == repeat.id
    assert first.sha256 != other.sha256 and first.sha256 != hashlib.sha256(data).hexdigest()
    async with session_scope() as s:
        await s.execute(
            first.__table__.update()
            .where(first.__table__.c.id == first.id)
            .values(sha256=hashlib.sha256(data).hexdigest(), digest_version=1)
        )
    await migration.migrate_content()
    assert (await documents.save_document("m1", "Belge", data, pages)).id == first.id


async def test_optional_document_hash_is_salted_and_dedup_survives_encryption(database):
    await repo.upsert_meeting("m1")
    first = await documents.save_document("m1", "Belge", b"data", [])
    assert first.sha256 != hashlib.sha256(b"data").hexdigest()
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()
    assert (await migration.migrate_content())["errors"] == 0
    repeated = await documents.save_document("m1", "Belge", b"data", [])
    assert repeated.id == first.id and repeated.digest_version == 2


async def test_forget_preserves_report_only_erasure(database, encrypted):
    person = await repo.ensure_person_by_email("a@example.com", "Ayşe")
    await repo.set_report("m1", {"summary": "Ayşe için karar"}, {})
    assert await repo.forget_person(person.id, requested_by="test")
    assert (await repo.get_meeting("m1")).report["summary"] == "Anonim katılımcı için karar"


async def test_forget_legacy_unbound_names_and_preserve_same_named_identity(database, encrypted):
    person = await repo.ensure_person_by_email("a@example.com", "Ayşe")
    for sid, name in (("s1", "Ayşe"), ("s2", "Başka")):
        await repo.save_segment("m1", segment(sid, speaker=name))
    async with session_scope() as s:
        await s.execute(MeetingSegment.__table__.update().values(speaker_name=""))
    for email in ("a@example.com", "b@example.com"):
        await repo.save_meeting_identity(
            "m1", ParticipantIdentity(name="Ayşe", email=email), "panel"
        )
    assert await repo.forget_person(person.id, requested_by="test")
    names = {s.id: s.speaker_name for s in (await repo.get_meeting("m1")).segments}
    assert names == {"s1": "Anonim katılımcı", "s2": "Başka"}
    assert [i.email for i in await repo.list_meeting_identities("m1")] == ["b@example.com"]


async def test_strict_reads_after_durable_completion_and_full_magic(database, encrypted, tmp_path):
    assert not crypto.is_encrypted(b"KMtext") and not crypto.is_encrypted(b"KM")
    assert not crypto.is_encrypted(b"KM\x03unsupported")
    assert crypto.is_encrypted(crypto.MAGIC_V1) and crypto.is_encrypted(crypto.MAGIC)
    await repo.upsert_meeting("m1", title="Başlık")
    await migration.migrate_content()
    get_settings().require_encryption = True
    path = tmp_path / "r.pdf"
    path.write_bytes(b"plaintext")
    with pytest.raises(crypto.DataKeyError, match="düz metin"):
        await crypto.read_file("m1", path)
    cipher = await crypto.meeting_cipher("m1")
    with pytest.raises(crypto.DataKeyError, match="düz metin"):
        cipher.read_file(path)
    with pytest.raises(crypto.DataKeyError, match="düz dosya"):
        cipher.migrate_file(path)
    async with session_scope() as s:
        await s.execute(update(Meeting).where(Meeting.id == "m1").values(encrypted_content=None))
    with pytest.raises(crypto.DataKeyError, match="düz metin"):
        await repo.get_meeting("m1")
    assert (await migration.migrate_content())["errors"] == 1
    with pytest.raises(crypto.DataKeyError, match="düz metin"):
        await repo.get_meeting("m1")
    await repo.upsert_meeting("m2", title="Yeni şifreli içerik")
    assert (await repo.get_meeting("m2")).title == "Yeni şifreli içerik"
    async with session_scope() as s:
        assert await s.scalar(select(EncryptionInstallation.migration_completed)) is True


async def test_forget_prefilters_batches_and_continues_past_bad_key(database, encrypted):
    person = await repo.ensure_person_by_email("a@example.com", "Ayşe")
    for i in range(105):
        await repo.save_segment("m1", segment(f"s{i:03}", speaker="Ayşe"), person.id)
    await repo.save_segment("bad", segment("bad", speaker="Ayşe"), person.id)
    await repo.save_segment("unrelated", segment("other", speaker="Başka"))
    async with session_scope() as s:
        await s.execute(
            update(MeetingDataKey)
            .where(MeetingDataKey.meeting_id == "bad")
            .values(key_id="unknown")
        )
    loaded = []

    def on_load(row, context):
        loaded.append(row.id)

    event.listen(MeetingSegment, "load", on_load)
    failures = []
    try:
        assert not await repo.forget_person(person.id, requested_by="test", failures=failures)
    finally:
        event.remove(MeetingSegment, "load", on_load)
    assert len(failures) == 1 and "other" not in loaded
    assert all(
        s.speaker_name == "Anonim katılımcı" for s in (await repo.get_meeting("m1")).segments
    )
    assert (await repo.get_person(person.id)).erasure_pending
    async with session_scope() as s:
        audit = await s.scalar(select(AuditLog).where(AuditLog.action == "forget"))
        assert audit.detail["segments_anonymized"] == 105
        assert audit.detail["failed_meetings"] == failures
    async with session_scope() as s:
        await s.execute(
            update(MeetingDataKey)
            .where(MeetingDataKey.meeting_id == "bad")
            .values(key_id=crypto.key_id(encrypted))
        )
    assert await repo.forget_person(person.id, requested_by="retry")
    assert await repo.get_person(person.id) is None
    assert (await repo.get_meeting("bad")).segments[0].speaker_name == "Anonim katılımcı"
