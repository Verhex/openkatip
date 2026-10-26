from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from katip import async_utils
from katip.api import security, ws
from katip.api.main import app
from katip.api.routes import meetings
from katip.documents import service as documents
from katip.events import Segment
from katip.pipeline import session as session_mod
from katip.pipeline.session import MeetingSession
from katip.pipeline.stt.base import STTProvider, Utterance
from katip.reports import service as reports
from katip.store import db, repo
from katip.workers import jobs


class EmptySTT(STTProvider):
    translates = True

    async def stream(self, audio, *, language, target_language=None):
        async for _ in audio:
            pass
        if False:
            yield


async def test_writer_cancellation_survives_database_cleanup_error(monkeypatch):
    entered = asyncio.Event()

    async def save(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            # Sürücünün kapanış hatası, asıl iptal istisnasının yerini alabilir.
            raise RuntimeError("bağlantı kapanış hatası") from exc

    monkeypatch.setattr(repo, "save_segment", save)
    session = MeetingSession("cancelled-writer", stt=EmptySTT(), persist=True)
    session._store(
        Segment(
            id="s1",
            session_id=session.session_id,
            text="Merhaba",
            is_final=True,
            start_s=0,
            end_s=1,
            lang="tr",
        )
    )
    writer = session._writer
    await asyncio.wait_for(entered.wait(), 1)
    writer.cancel()
    try:
        done, _ = await asyncio.wait({writer}, timeout=0.2)
        assert writer in done, "İptal edilen yazıcı yeniden kuyruk beklememeli"
        assert writer.cancelled()
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


def test_database_pool_is_not_reused_across_event_loops():
    engines = []

    async def use_database():
        async with db.session_scope() as session:
            engines.append(session.bind)
            await session.execute(text("SELECT 1"))

    async def second_loop():
        try:
            await use_database()
        finally:
            await db.dispose()

    first = asyncio.new_event_loop()
    try:
        first.run_until_complete(use_database())
        asyncio.run(second_loop())
    finally:
        first.run_until_complete(engines[0].dispose())
        first.close()
    assert engines[0] is not engines[1], "Bağlantı havuzunun kilitleri döngüye aittir"


def test_synchronous_configuration_never_transfers_a_used_pool_to_another_loop():
    configured = db.configure()
    first = asyncio.new_event_loop()

    async def use_configured_engine():
        async with configured.begin() as connection:
            await connection.execute(text("SELECT 1"))

    async def use_next_loop():
        try:
            async with db.session_scope() as session:
                assert session.bind is not configured
                await session.execute(text("SELECT 1"))
        finally:
            await db.dispose()

    try:
        first.run_until_complete(use_configured_engine())
        asyncio.run(use_next_loop())
    finally:
        first.run_until_complete(configured.dispose())
        first.close()


@pytest.mark.parametrize("cache", [documents._write_lock, meetings._join_lock, jobs._claim_lock])
def test_contended_global_lock_is_local_to_each_event_loop(cache):
    locks = []

    async def contend():
        lock = cache.get()
        locks.append(lock)
        async with lock:
            waiter = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        # İptal edilen bekleyici kilidi elde tutmamalı.
        async with lock:
            pass

    asyncio.run(contend())
    asyncio.run(contend())
    assert locks[0] is not locks[1]


async def _stubborn(release, entered):
    entered.set()
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            continue


async def test_report_stop_is_bounded_and_keeps_unfinished_work_tracked(monkeypatch):
    monkeypatch.setattr(reports, "_STOP_TIMEOUT_S", 0.05)
    release, entered = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_stubborn(release, entered))
    reports.track_report("stuck-report", task)
    await entered.wait()
    try:
        with pytest.raises(TimeoutError, match="silme işlemi"):
            await asyncio.wait_for(reports.stop_reports("stuck-report"), 1)
        assert task in reports._tasks.get()["stuck-report"]
    finally:
        release.set()
        await task


async def test_session_shutdown_bounds_full_audio_queue_and_stuck_workers(monkeypatch):
    monkeypatch.setattr(session_mod, "_SHUTDOWN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(session_mod, "_CANCEL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(async_utils, "_CANCEL_GRACE_S", 0.05)
    session = MeetingSession("stuck-session", stt=EmptySTT(), persist=False)
    release, entered = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_stubborn(release, entered))
    session._stt_task = task
    while not session._audio.full():
        session._audio.put_nowait(b"pcm")
    await entered.wait()
    try:
        await asyncio.wait_for(session.shutdown(), 1)
        session.push_audio(b"late")
        assert session._stt_task is task
        assert session._shutdown_task.done()
    finally:
        release.set()
        await task


async def test_ingest_disconnect_bounds_sender_that_ignores_cancellation(monkeypatch):
    monkeypatch.setattr(ws, "_SENDER_STOP_TIMEOUT_S", 0.05)
    session = session_mod.registry._sessions["stuck-sender"] = MeetingSession(
        "stuck-sender", stt=EmptySTT(), persist=False
    )
    release, entered = asyncio.Event(), asyncio.Event()
    tasks = []

    async def forward(*args):
        tasks.append(asyncio.current_task())
        await _stubborn(release, entered)

    class Socket:
        headers = {"x-katip-bot-token": security.issue_bot_token(session.session_id)}
        client = type("Client", (), {"host": "127.0.0.1"})()

        async def accept(self):
            pass

        async def receive(self):
            await entered.wait()
            return {"type": "websocket.disconnect"}

    monkeypatch.setattr(ws, "_forward_to_bot", forward)
    try:
        await asyncio.wait_for(ws.ingest(Socket(), session.session_id), 1)
        assert not tasks[0].done()
    finally:
        release.set()
        await tasks[0]


def test_sequential_testclient_lifespans_drain_work_before_ingest_disconnect(monkeypatch):
    monkeypatch.setattr(session_mod, "build_provider", EmptySTT)
    work = []

    async def seed():
        session = session_mod.registry.create("shutdown-regression")
        session._on_utterance(Utterance("s1", "Merhaba", True, 0, 1, "1", "tr"))
        task = asyncio.create_task(asyncio.Event().wait())
        reports.track_report(session.session_id, task, batch=True)
        work.extend((session, task))
        await asyncio.sleep(0)

    with TestClient(app, backend_options={"debug": True}) as client:
        client.portal.call(seed)
    session, report = work
    assert session._writer.done()
    assert report.cancelled()
    assert session_mod.registry.get(session.session_id) is None

    with TestClient(app, backend_options={"debug": True}) as client:
        client.portal.call(session_mod.registry.create, "shutdown-regression")
        token = security.issue_bot_token("shutdown-regression")
        with client.websocket_connect(
            "/ws/ingest/shutdown-regression", headers={"X-Katip-Bot-Token": token}
        ):
            started = time.monotonic()
        assert time.monotonic() - started < 2


def test_report_tracking_does_not_await_tasks_from_previous_loop():
    first = asyncio.new_event_loop()
    tasks = []

    async def seed():
        task = asyncio.create_task(asyncio.Event().wait())
        reports.track_report("same-meeting", task, batch=True)
        tasks.append(task)
        await asyncio.sleep(0)

    async def clean():
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        first.run_until_complete(seed())
        asyncio.run(reports.stop_reports("same-meeting"))
        assert not tasks[0].done()
    finally:
        first.run_until_complete(clean())
        first.close()


def test_loop_local_works_without_loop_dict_like_uvloop(monkeypatch):
    """uvloop döngüsünde __dict__ yok; LoopLocal döngü nesnesine yazmamalı."""
    from katip import async_utils

    class SlotLoop:
        __slots__ = ("closed",)

        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

    first, second = SlotLoop(), SlotLoop()
    current = [first]
    monkeypatch.setattr(async_utils.asyncio, "get_running_loop", lambda: current[0])
    local = async_utils.LoopLocal(list)
    local.get().append("a")
    assert local.get() == ["a"]
    current[0] = second
    assert local.get() == []  # her döngünün kendi durumu var
    local.set(["b"])
    assert local.get() == ["b"]
    first.closed = True
    current[0] = SlotLoop()  # yeni döngü görülünce kapalı döngünün durumu bırakılır
    assert local.get() == []
    assert all(loop is not first for loop, _ in local._states.values())
