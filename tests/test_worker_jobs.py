from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from katip.api import security
from katip.api.main import app
from katip.async_utils import LoopLocal
from katip.config import get_settings
from katip.pipeline.session import registry
from katip.store import db, repo
from katip.store.db import session_scope
from katip.store.models import WorkerJob
from katip.workers import jobs, manager

KEY = "manager-secret-" + "x" * 40
SID = "a" * 12
ADMIN = {"Authorization": "Bearer test-admin"}
WORKER = {"X-Katip-Worker-Id": "host1", "Authorization": "Bearer " + KEY}
SPEC = {
    "url": "https://meet.google.com/abc-defg-hij",
    "source_lang": "tr",
    "target_lang": None,
    "mode": "kayit",
    "max_minutes": 10,
    "participants": [],
}


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    settings = get_settings()
    settings.bot_backend = "remote"
    settings.worker_manager_keys = {"host1": KEY, "host2": KEY + "2"}
    monkeypatch.setattr(jobs, "_claim_lock", LoopLocal(asyncio.Lock))
    monkeypatch.setattr(registry, "_sessions", {})
    monkeypatch.setattr(security, "_BOT_TOKENS", {})
    monkeypatch.setattr(security, "_BOT_EXPIRIES", {})


async def queue(sid=SID):
    await repo.upsert_meeting(sid, state="joining", url=SPEC["url"])
    await jobs.enqueue(sid, SPEC, 10)


async def expire(sid=SID, *, seconds=1):
    async with session_scope() as session:
        await session.execute(
            update(WorkerJob)
            .where(WorkerJob.meeting_id == sid)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=seconds))
        )


async def test_claim_is_exclusive_and_tokens_not_stored_plaintext(database):
    await queue()
    results = await asyncio.gather(jobs.claim("host1"), jobs.claim("host2"))
    assert sum(result is not None for result in results) == 1
    row, token = next(result for result in results if result is not None)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert security.check_bot_token(SID, token)
    assert not security.check_bot_token("b" * 12, token)
    assert token not in str(row.spec) and token not in str(jobs.public_status(row))
    assert await jobs.count_open() == 1


async def test_one_manager_cannot_claim_a_second_call(database):
    await queue()
    await queue("b" * 12)
    assert await jobs.claim("host1")
    assert await jobs.claim("host1") is None
    assert await jobs.claim("host2")


async def test_cancel_revokes_token_and_requires_owner_stop_ack(database):
    await queue()
    row, token = await jobs.claim("host1")
    assert await jobs.request_cancel(SID) == "stopping"
    assert not security.check_bot_token(SID, token)
    assert not await jobs.heartbeat(SID, "host1", row.lease_id)
    assert not await jobs.finish(SID, "host2", row.lease_id, 0)
    assert await jobs.count_open() == 1
    assert await jobs.finish(SID, "host1", row.lease_id, 1)
    assert await jobs.finish(SID, "host1", row.lease_id, 1)  # lost response retry
    assert (await jobs.get(SID)).status == "cancelled"
    assert await jobs.count_open() == 0


async def test_expired_lease_cannot_renew_or_auto_rejoin(database):
    await queue()
    row, token = await jobs.claim("host1")
    await expire(seconds=100)
    assert not await jobs.heartbeat(SID, "host1", row.lease_id)
    await jobs.sweep()
    await jobs.sweep()
    lost = await jobs.get(SID)
    assert lost.status == "lost" and not jobs.public_status(lost)["stop_confirmed"]
    assert await jobs.count_open() == 1  # capacity retained until proof
    assert not security.check_bot_token(SID, token)
    assert await jobs.claim("host2") is None
    assert await jobs.finish(SID, "host1", row.lease_id, 1)
    assert await jobs.count_open() == 0


async def test_queued_job_cancel_never_needs_worker_ack(database):
    await queue()
    assert await jobs.request_cancel(SID) == "cancelled"
    assert await jobs.claim("host1") is None
    assert await jobs.count_open() == 0


async def test_manual_resolution_is_explicit_and_audited(database):
    await queue()
    row, _ = await jobs.claim("host1")
    assert not await jobs.resolve_unknown(SID)  # Cannot override a live job.
    await expire(seconds=100)
    await jobs.sweep()
    await jobs.sweep()
    assert await jobs.resolve_unknown(SID)
    closed = jobs.public_status(await jobs.get(SID))
    assert closed["manually_closed"] and not closed["stop_confirmed"]
    assert await jobs.count_open() == 0
    assert len(await repo.recent_audit(("worker_manually_closed",))) == 1
    assert not await jobs.resolve_unknown(SID)


async def test_database_restart_preserves_queued_spec(database, monkeypatch):
    from test_pipeline import StubSoniox

    monkeypatch.setattr("katip.pipeline.session.build_provider", lambda: StubSoniox())
    await queue()
    await db.dispose()
    db.configure()
    await db.init_db()
    await jobs.recover()
    assert (await jobs.get(SID)).status == "queued"
    session = registry.get(SID)
    assert session and session.mode == "kayit" and session.target_lang is None
    await registry.drop(SID)


async def test_restart_fences_running_calls(database):
    await queue()
    row, token = await jobs.claim("host1")
    await jobs.recover()
    assert (await jobs.get(SID)).status == "stopping"
    assert not security.check_bot_token(SID, token)
    assert not await jobs.heartbeat(SID, "host1", row.lease_id)
    assert await jobs.claim("host2") is None


async def test_live_manager_cannot_hide_a_lost_browser_connection(database):
    await queue()
    row, token = await jobs.claim("host1")
    async with session_scope() as session:
        await session.execute(
            update(WorkerJob)
            .where(WorkerJob.meeting_id == SID)
            .values(ingest_at=datetime.now(UTC) - timedelta(seconds=100))
        )
    assert await jobs.heartbeat(SID, "host1", row.lease_id)
    await jobs.sweep()
    current = await jobs.get(SID)
    assert current.status == "stopping" and current.reason == "ingest_lost"
    assert not security.check_bot_token(SID, token)


def test_worker_api_has_narrow_auth_and_redacted_status(monkeypatch):
    # Lifespan reloads settings; fixture uses a single cached object.
    with TestClient(app) as client:
        assert client.post("/api/workers/claim", headers=ADMIN, json={}).status_code == 401
        assert client.post("/api/workers/claim", headers=WORKER, json={}).json() == {"job": None}
        assert client.get("/api/workers/jobs", headers=WORKER).status_code == 401
        assert client.get("/api/workers/jobs", headers=ADMIN).status_code == 200
        assert client.get("/api/admin/overview", headers=WORKER).status_code == 401
        assert (
            client.post(
                "/api/workers/jobs/" + SID + "/resolve",
                headers=WORKER,
                json={"confirmation": "worker_stop_verified"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/workers/finish",
                headers=WORKER,
                json={
                    "session_id": SID,
                    "lease_id": "b" * 48,
                    "exit_code": 0,
                },
            ).status_code
            == 409
        )


def test_remote_queue_end_to_end(monkeypatch):
    from katip.bot.supervisor import supervisor

    get_settings().soniox_api_key = "test-key"

    async def no_launch(*args, **kwargs):
        return

    monkeypatch.setattr(supervisor, "start", no_launch)
    with TestClient(app) as client:
        joined = client.post("/api/meetings", headers=ADMIN, json={"url": SPEC["url"]})
        assert joined.status_code == 200
        sid = joined.json()["session_id"]
        claimed = client.post("/api/workers/claim", headers=WORKER, json={}).json()["job"]
        assert claimed["session_id"] == sid
        assert "SONIOX_API_KEY" not in claimed["environment"]
        assert "KATIP_ADMIN_TOKEN" not in claimed["environment"]
        assert client.post("/api/workers/claim", headers=WORKER, json={}).json()["job"] is None
        with client.websocket_connect(
            "/ws/ingest/" + sid,
            headers={"X-Katip-Bot-Token": claimed["environment"]["KATIP_BOT_TOKEN"]},
        ) as socket:
            socket.send_text('{"kind":"heartbeat"}')
            assert socket.receive_json() == {"kind": "heartbeat_ack"}
        row = client.get("/api/workers/jobs", headers=ADMIN).json()[0]
        assert "lease_id" not in row and "token_hash" not in row and "spec" not in row
        stop = client.delete("/api/meetings/" + sid, headers=ADMIN).json()
        assert stop["worker_status"] == "stopping" and not stop["stop_confirmed"]
        payload = {"session_id": sid, "lease_id": claimed["lease_id"]}
        assert not client.post("/api/workers/heartbeat", headers=WORKER, json=payload).json()[
            "continue"
        ]
        assert (
            client.post(
                "/api/workers/finish",
                headers=WORKER,
                json={
                    **payload,
                    "exit_code": 1,
                    "stop_confirmed": True,
                },
            ).status_code
            == 200
        )
        assert client.get("/api/workers/jobs", headers=ADMIN).json()[0]["stop_confirmed"]


def test_graceful_api_restart_preserves_unclaimed_jobs(monkeypatch):
    from katip.bot.supervisor import supervisor

    get_settings().soniox_api_key = "test-key"

    async def no_launch(*args, **kwargs):
        pass

    monkeypatch.setattr(supervisor, "start", no_launch)
    with TestClient(app) as client:
        joined = client.post("/api/meetings", headers=ADMIN, json={"url": SPEC["url"]})
        sid = joined.json()["session_id"]
    assert registry.get(sid) is None
    with TestClient(app) as client:
        status = client.get("/api/workers/jobs", headers=ADMIN).json()[0]
        assert status["status"] == "queued"
        assert registry.get(sid) is not None
        job = client.post("/api/workers/claim", headers=WORKER, json={}).json()["job"]
        assert job["session_id"] == sid


@pytest.mark.parametrize(
    "url",
    [
        "http://worker.example",
        "https://u:p@example.com",
        "https://example.com/path",
        "https://example.com?token=x",
    ],
)
def test_manager_rejects_insecure_or_credential_bearing_api_urls(url):
    with pytest.raises(ValueError):
        manager.api_url(url)


def test_manager_rejects_server_secrets_in_job_environment():
    job = {
        "url": SPEC["url"],
        "max_minutes": 10,
        "environment": {"SONIOX_API_KEY": "must-not-pass"},
    }
    with pytest.raises(ValueError):
        manager.environment(job, "https://api.example.com")


async def test_manager_http_outage_expires_original_lease_and_removes_browser(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: clock.now))
    removed, finished = [], []

    class Proc:
        returncode = None

        async def wait(self):
            if self.returncode is not None:
                return self.returncode
            clock.now += 20
            raise TimeoutError

        def terminate(self):
            self.returncode = -15

    async def spawn(*args, **kwargs):
        return Proc()

    async def docker(*args, **kwargs):
        assert args[0] == "create"
        assert "--mount" not in args
        return 0, b"container-id", b""

    async def remove(name):
        removed.append(name)

    async def request(path, payload=None):
        if path == "heartbeat":
            raise httpx.ConnectError("offline")
        finished.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(manager.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(manager, "docker_call", docker)
    monkeypatch.setattr(manager, "remove_container", remove)
    worker = manager.Manager(None, "https://api.example.com")
    monkeypatch.setattr(worker, "request", request)
    await worker.execute(
        {
            "session_id": SID,
            "lease_id": "b" * 48,
            "url": SPEC["url"],
            "max_minutes": 10,
            "lease_s": 60,
            "environment": {"KATIP_BOT_TOKEN": "t" * 40},
        },
        0,
    )
    assert clock.now == 60
    assert removed == ["katip-meeting-" + SID]
    assert finished[0][1]["stop_confirmed"] is True


async def test_failed_docker_removal_never_acknowledges_stop(monkeypatch):
    async def docker(*args, **kwargs):
        return 1, b"", b"failed"

    async def remove(name):
        raise RuntimeError("daemon inaccessible")

    acknowledged = []

    async def ack(*args):
        acknowledged.append(args)

    worker = manager.Manager(None, "https://api.example.com")
    monkeypatch.setattr(manager, "docker_call", docker)
    monkeypatch.setattr(manager, "remove_container", remove)
    monkeypatch.setattr(worker, "acknowledge", ack)
    with pytest.raises(RuntimeError, match="daemon inaccessible"):
        await worker.execute(
            {
                "session_id": SID,
                "lease_id": "b" * 48,
                "url": SPEC["url"],
                "max_minutes": 10,
                "lease_s": 60,
                "environment": {"KATIP_BOT_TOKEN": "t" * 40},
            },
            0,
        )
    assert not acknowledged


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image", "untrusted:latest"),
        ("image", "katip-worker:local"),  # Aynı imaj bile işten seçilemez.
        ("mounts", [{"source": "/", "target": "/host"}]),
        ("volumes", ["/var/run/docker.sock:/var/run/docker.sock"]),
        ("privileged", True),
        ("devices", ["/dev/sda"]),
        ("network_mode", "host"),
        ("command", ["sh"]),
        ("environment", {"KATIP_BOT_TOKEN": "t" * 40, "LD_PRELOAD": "/evil.so"}),
        ("environment", {"KATIP_BOT_TOKEN": "t" * 40, "KATIP_DATA_DIR": "/data"}),
        ("environment", {"KATIP_BOT_TOKEN": "t" * 40, "DOCKER_HOST": "tcp://evil"}),
    ],
)
async def test_manager_rejects_job_launch_injection_before_any_docker_call(
    monkeypatch, field, value
):
    async def never_call(*args, **kwargs):
        pytest.fail("Reddedilen iş Docker'a erişti")

    monkeypatch.setattr(manager, "docker_call", never_call)
    monkeypatch.setattr(manager, "remove_container", never_call)
    job = {
        "session_id": SID,
        "lease_id": "b" * 48,
        "url": SPEC["url"],
        "max_minutes": 10,
        "lease_s": 60,
        "environment": {"KATIP_BOT_TOKEN": "t" * 40},
        field: value,
    }
    with pytest.raises(ValueError):
        await manager.Manager(None, "https://api.example.com").execute(job, 0)


def test_http_exception_requires_explicit_exact_internal_service_name():
    with pytest.raises(ValueError):
        manager.api_url("http://api:8000")
    assert manager.api_url("http://api:8000/", "api") == "http://api:8000"
    assert manager.api_url("https://remote.example") == "https://remote.example"
    assert manager.api_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for value in ("http://api.evil:8000", "http://worker.example", "http://api@evil:8000"):
        with pytest.raises(ValueError):
            manager.api_url(value, "api")
    for hostname in ("*", "remote.example", "127.0.0.1", "api:8000"):
        with pytest.raises(ValueError):
            manager.api_url("http://api:8000", hostname)
