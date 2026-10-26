"""Toplantı izinleri: panel listeleri, işlemler, indirmeler ve canlı bağlantılar."""

from __future__ import annotations

import time
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from katip.api import auth, security
from katip.api.main import app
from katip.api.routes import documents, meetings
from katip.events import Segment, SessionState
from katip.pipeline.session import registry
from katip.store import db, repo
from katip.workers import jobs

ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = "Katip-Test-Password-2026!"


@pytest.mark.parametrize("action", ["auto", "pause", "resume"])
def test_presentation_actions_permissions_and_status(acl, monkeypatch, action):
    client, accounts, sid, _ = acl
    state = {"page": 2, "pages": 6, "mode": "auto", "state": "paused", "error": None}
    calls = []

    async def control(name, page):
        calls.append((name, page))
        return state

    live = SimpleNamespace(presentation_state=state, presentation_action=control)
    monkeypatch.setattr(registry, "get", lambda mid: live)
    path = f"/api/meetings/{sid}/presentation"
    headers = login(client, "other@example.com")
    assert client.get(path).status_code == 404
    assert client.post(path, headers=headers, json={"action": action}).status_code == 404
    client.post(
        f"/api/meetings/{sid}/grants",
        headers=ADMIN,
        json={"account_id": accounts["other"], "permission": "read"},
    )
    assert client.get(path).json() == state
    assert client.post(path, headers=headers, json={"action": action}).status_code == 404
    assert not calls
    client.post(
        f"/api/meetings/{sid}/grants",
        headers=ADMIN,
        json={"account_id": accounts["other"], "permission": "manage"},
    )
    assert client.post(path, headers=headers, json={"action": action}).json() == state
    assert calls == [(action, None)]


def login(client, email):
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


@pytest.fixture
def acl(monkeypatch, tmp_path):
    async def parse(data, kind):
        return [{"location": "bölüm 1", "text": data.decode()}]

    monkeypatch.setattr(documents.service, "parse_document", parse)
    with TestClient(app, base_url="https://testserver") as client:
        accounts = {}
        for name, role in (
            ("owner", "operator"),
            ("other", "operator"),
            ("viewer", "viewer"),
            ("admin", "admin"),
        ):
            response = client.post(
                "/api/auth/accounts",
                headers=ADMIN,
                json={
                    "email": f"{name}@example.com",
                    "password": PASSWORD,
                    "role": role,
                },
            )
            assert response.status_code == 200, response.text
            accounts[name] = response.json()["id"]
        headers = login(client, "owner@example.com")
        sid = client.post("/api/preparations", json={}, headers=headers).json()["session_id"]
        other = client.post("/api/preparations", json={}, headers=ADMIN).json()["session_id"]
        assert (
            client.post(
                f"/api/preparations/{sid}/documents?filename=ürün.txt",
                content="Gizli ürün".encode(),
                headers=headers,
            ).status_code
            == 200
        )
        files = {}
        for fmt in ("pdf", "docx"):
            path = tmp_path / f"rapor.{fmt}"
            path.write_bytes(b"private report")
            files[fmt] = str(path)
        client.portal.call(repo.set_report, sid, {"title": "Gizli toplantı"}, files)
        client.portal.call(partial(repo.add_usage, sid, llm_calls=1, llm_usd=1.0))
        client.portal.call(
            repo.save_segment,
            sid,
            Segment(
                id="segment",
                session_id=sid,
                start_s=0,
                end_s=1,
                text="Gizli konuşma",
                lang="tr",
                is_final=True,
            ),
        )
        image = tmp_path / "ekran.jpg"
        image.write_bytes(b"private screenshot")
        client.portal.call(repo.add_asset, sid, "screenshot", 1, str(image), 0, None)
        client.portal.call(jobs.enqueue, sid, {"url": "https://meet.google.com/abc-defg-hij"}, 60)
        live = SimpleNamespace(
            session_id=sid,
            state=SessionState.ACTIVE,
            mode="tam",
            _roster={"Ayşe": None},
            final_segments=lambda: [],
            reply_mode="chat",
            paused=False,
            _created_at=time.monotonic(),
            target_lang=None,
        )
        monkeypatch.setattr(registry, "all", lambda: [live])
        yield client, accounts, sid, other
        monkeypatch.setattr(registry, "all", lambda: [])


@pytest.mark.parametrize("name", ["other", "viewer"])
def test_ungranted_accounts_cannot_read_meetings_and_lists_are_empty(acl, name):
    client, _, sid, _ = acl
    login(client, f"{name}@example.com")
    for path in (
        f"/panel/{sid}",
        f"/api/meetings/{sid}/transcript",
        f"/api/meetings/{sid}/report",
        f"/api/meetings/{sid}/report.pdf",
        f"/api/meetings/{sid}/report.docx",
        f"/api/meetings/{sid}/screenshots/1",
        f"/api/meetings/{sid}/participants",
        f"/api/preparations/{sid}/documents",
        f"/api/usage/{sid}",
    ):
        response = client.get(path)
        assert response.status_code == 404, (path, response.text)
    assert client.get("/api/meetings").json() == []
    assert client.get("/api/admin/meetings").json() == {"meetings": []}
    usage = client.get("/api/usage").json()
    assert usage["meetings"] == [] and usage["totals"]["usd"]["total"] == 0
    assert client.get("/api/workers/jobs").json() == []
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f"wss://testserver/ws/panel/{sid}"):
            pass
    assert error.value.code == 1008


@pytest.mark.parametrize("name", ["other", "viewer"])
def test_ungranted_accounts_cannot_change_any_meeting_route(acl, name):
    client, accounts, sid, _ = acl
    headers = login(client, f"{name}@example.com")
    expected = 403 if name == "viewer" else 404
    for method, path, body in (
        ("DELETE", f"/api/meetings/{sid}", None),
        ("POST", f"/api/meetings/{sid}/mode", {"mode": "chat"}),
        ("POST", f"/api/meetings/{sid}/presentation", {"action": "start"}),
        ("POST", f"/api/meetings/{sid}/share", None),
        ("POST", f"/api/meetings/{sid}/report", None),
        ("DELETE", f"/api/preparations/{sid}", None),
        ("POST", f"/api/preparations/{sid}/documents?filename=x.txt", None),
        ("DELETE", f"/api/preparations/{sid}/documents/missing", None),
        (
            "POST",
            f"/api/meetings/{sid}/grants",
            {"account_id": accounts["viewer"], "permission": "read"},
        ),
        ("DELETE", f"/api/meetings/{sid}/grants/{accounts['viewer']}", None),
        (
            "POST",
            "/api/meetings",
            {"url": "https://meet.google.com/abc-defg-hij", "preparation_id": sid},
        ),
    ):
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == expected, (path, response.text)
    assert client.get(f"/api/meetings/{sid}/grants").status_code == expected
    for method, suffix, body in (
        ("GET", "debug", None),
        ("GET", "debug.png", None),
        ("GET", "debug.html", None),
        ("POST", "diagnose", None),
        ("PUT", "participants", {"participants": []}),
        ("PUT", "participants/binding", {"name": "Ayşe", "email": "ayse@example.com"}),
        ("POST", "participants/microsoft-sync", None),
    ):
        assert (
            client.request(
                method, f"/api/meetings/{sid}/{suffix}", json=body, headers=headers
            ).status_code
            == 404
        )
    assert client.get("/api/approvals").status_code == 403
    assert client.post("/api/approvals/missing/approve", headers=headers).status_code == 403
    assert client.post("/api/approvals/missing/reject", headers=headers).status_code == 403
    assert (
        client.post(
            f"/api/workers/jobs/{sid}/resolve",
            headers=headers,
            json={"confirmation": "worker_stop_verified"},
        ).status_code
        == 404
    )


def test_read_and_manage_grants_owner_and_admin_boundaries(acl, monkeypatch):
    client, accounts, sid, other = acl
    owner_headers = login(client, "owner@example.com")
    assert client.get(f"/api/meetings/{sid}/grants").json()["owner_account_id"] == accounts["owner"]
    assert client.get(f"/api/meetings/{other}/transcript").status_code == 404
    path = f"/api/meetings/{sid}/grants"
    assert (
        client.post(path, json={"account_id": accounts["other"], "permission": "read"}).status_code
        == 403
    )
    assert (
        client.post(
            path,
            headers=owner_headers,
            json={
                "email": "other@example.com",
                "permission": "read",
            },
        ).status_code
        == 200
    )
    headers = login(client, "other@example.com")
    assert client.get(f"/api/meetings/{sid}/transcript").json()[0]["text"] == "Gizli konuşma"
    assert client.get(f"/api/usage/{sid}").status_code == 200
    assert client.get(f"/api/preparations/{sid}/documents").status_code == 200
    assert client.get("/api/meetings").json()[0]["can_manage"] is False
    history = client.get("/api/admin/meetings?limit=1").json()["meetings"]
    assert [m["id"] for m in history] == [sid]
    assert "?t=" not in history[0]["report_pdf"]
    assert client.get("/api/usage").json()["totals"]["meetings"] == 1
    assert len(client.get("/api/workers/jobs").json()) == 1
    assert client.post(f"/api/meetings/{sid}/share", headers=headers).status_code == 404
    assert (
        client.post(
            path,
            headers=ADMIN,
            json={
                "account_id": accounts["other"],
                "permission": "manage",
            },
        ).status_code
        == 200
    )
    assert client.get("/api/meetings").json()[0]["can_manage"] is True
    assert client.post(f"/api/meetings/{sid}/share", headers=headers).status_code == 200
    assert client.get(path).status_code == 403
    assert (
        client.post(
            path,
            headers=headers,
            json={
                "account_id": accounts["viewer"],
                "permission": "read",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/preparations/{sid}/documents?filename=new.txt", content=b"new", headers=headers
        ).status_code
        == 200
    )

    async def generate(session_id):
        return {"pdf": "fake"}

    monkeypatch.setattr(meetings, "generate_report", generate)
    assert client.post(f"/api/meetings/{sid}/report", headers=headers).status_code == 200
    assert (
        client.post(
            path,
            headers=ADMIN,
            json={
                "account_id": accounts["viewer"],
                "permission": "manage",
            },
        ).status_code
        == 200
    )
    viewer_headers = login(client, "viewer@example.com")
    assert client.get(f"/api/meetings/{sid}/report.pdf").content == b"private report"
    assert client.post(f"/api/meetings/{sid}/report", headers=viewer_headers).status_code == 403
    assert client.post("/api/preparations", json={}, headers=viewer_headers).status_code == 403
    assert client.delete(path + f"/{accounts['viewer']}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/meetings/{sid}/transcript").status_code == 404
    events = client.portal.call(repo.recent_audit, ("meeting_grant_added", "meeting_grant_removed"))
    assert len(events) == 4 and events[0].action == "meeting_grant_removed"
    assert any(e.actor == f"panel:{accounts['owner']}" for e in events)
    login(client, "admin@example.com")
    assert len(client.get("/api/admin/meetings").json()["meetings"]) == 2
    assert client.get(f"/api/meetings/{other}/transcript").status_code == 200


def test_panel_websocket_and_capability_links_keep_their_scope(acl):
    client, accounts, sid, other = acl
    login(client, "viewer@example.com")
    ticket = security.sign(sid, ttl_s=60)
    assert client.get(f"/api/meetings/{sid}/report.pdf?t={ticket}").status_code == 200
    assert client.get(f"/api/meetings/{other}/transcript?t={ticket}").status_code == 404
    with client.websocket_connect(f"wss://testserver/ws/panel/{sid}?t={ticket}") as ws:
        assert ws.receive_json()["segment"]["text"] == "Gizli konuşma"
    assert (
        client.post(
            f"/api/meetings/{sid}/grants",
            headers=ADMIN,
            json={
                "account_id": accounts["viewer"],
                "permission": "read",
            },
        ).status_code
        == 200
    )
    with client.websocket_connect(f"wss://testserver/ws/panel/{sid}") as ws:
        assert ws.receive_json()["kind"] == "segment"
    client.cookies.clear()
    for invalid in (
        security.sign(sid, ttl_s=-1),
        security.sign(sid, ttl_s=60, purpose="bot"),
        ticket + "x",
    ):
        assert client.get(f"/api/meetings/{sid}/transcript?t={invalid}").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"wss://testserver/ws/panel/{sid}?t={invalid}"):
                pass
    assert client.get(f"/panel/{sid}?t={ticket}").status_code == 200


def test_panel_start_records_owner_and_calendar_start_has_no_owner(acl, monkeypatch):
    client, accounts, _, _ = acl
    owner_headers = login(client, "owner@example.com")
    prepared = client.post("/api/preparations", json={}, headers=owner_headers).json()["session_id"]
    headers = login(client, "other@example.com")
    # Başlatma yolunu çalıştırırken gerçek bot, STT ve harici bağlantı kullanılmaz.
    session = SimpleNamespace(source_lang="tr", target_lang=None, mode="tam", max_minutes=60)
    monkeypatch.setattr(registry, "create", lambda *args, **kwargs: session)
    monkeypatch.setattr(jobs, "count_open", lambda: _zero())

    async def start(*args, **kwargs):
        pass

    monkeypatch.setattr(meetings.supervisor, "start", start)
    body = {"url": "https://meet.google.com/abc-defg-hij"}
    assert (
        client.post(
            f"/api/meetings/{prepared}/grants",
            headers=ADMIN,
            json={
                "account_id": accounts["other"],
                "permission": "manage",
            },
        ).status_code
        == 200
    )
    response = client.post(
        "/api/meetings", json={**body, "preparation_id": prepared}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert client.portal.call(repo.get_meeting, prepared).owner_account_id == accounts["other"]
    response = client.post("/api/meetings", json=body, headers=headers)
    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    assert client.portal.call(repo.get_meeting, sid).owner_account_id == accounts["other"]
    assert client.get(f"/api/meetings/{sid}/transcript").status_code == 200
    result = client.portal.call(meetings.start_meeting, meetings.JoinRequest(**body))
    assert client.portal.call(repo.get_meeting, result.session_id).owner_account_id is None
    assert client.get(f"/api/meetings/{result.session_id}/transcript").status_code == 404


async def _zero():
    return 0


def test_manage_grant_controls_mode_and_stop_but_read_grant_cannot(acl, monkeypatch):
    client, accounts, sid, _ = acl
    headers = login(client, "other@example.com")
    for permission in ("read", "manage"):
        assert (
            client.post(
                f"/api/meetings/{sid}/grants",
                headers=ADMIN,
                json={
                    "account_id": accounts["other"],
                    "permission": permission,
                },
            ).status_code
            == 200
        )
        if permission == "read":
            assert (
                client.post(
                    f"/api/meetings/{sid}/mode", headers=headers, json={"mode": "voice"}
                ).status_code
                == 404
            )
            assert client.delete(f"/api/meetings/{sid}", headers=headers).status_code == 404
            assert client.post(f"/api/meetings/{sid}/report", headers=headers).status_code == 404
            assert (
                client.post(
                    f"/api/meetings/{sid}/presentation", headers=headers, json={"action": "start"}
                ).status_code
                == 404
            )
            assert client.delete(f"/api/preparations/{sid}", headers=headers).status_code == 404
    live = registry.all()[0]
    live.set_reply_mode = lambda mode: setattr(live, "reply_mode", mode)

    async def presentation_action(action, page):
        return {"page": 1, "pages": 6, "error": None}

    live.presentation_action = presentation_action
    monkeypatch.setattr(registry, "get", lambda mid: live if mid == sid else None)
    stopped = []

    async def stop(mid):
        stopped.append(mid)
        return True

    async def drop(mid):
        pass

    monkeypatch.setattr(meetings.supervisor, "stop", stop)
    monkeypatch.setattr(registry, "drop", drop)
    assert client.post(
        f"/api/meetings/{sid}/mode", headers=headers, json={"mode": "voice"}
    ).json() == {"mode": "voice"}
    assert (
        client.post(
            f"/api/meetings/{sid}/presentation", headers=headers, json={"action": "start"}
        ).json()["page"]
        == 1
    )
    result = client.delete(f"/api/meetings/{sid}", headers=headers)
    assert result.status_code == 200 and result.json()["stopped"] is True
    assert stopped == [sid]


def test_viewer_role_does_not_inherit_owner_management(acl):
    client, accounts, sid, _ = acl
    login(client, "owner@example.com")
    assert client.portal.call(repo.get_meeting, sid).owner_account_id == accounts["owner"]
    client.portal.call(
        partial(
            repo.set_panel_account, "owner@example.com", auth.hash_password(PASSWORD), role="viewer"
        )
    )
    assert client.get("/api/auth/me").status_code == 401
    login(client, "owner@example.com")
    assert client.get(f"/api/meetings/{sid}/transcript").status_code == 404
    assert client.get("/api/meetings").json() == []


async def test_existing_meetings_migrate_to_ownerless_and_grants_cascade():
    engine = db.configure()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                CREATE TABLE meetings (
                    id VARCHAR(64) PRIMARY KEY, platform VARCHAR(16), url TEXT,
                    title VARCHAR(300), source_lang VARCHAR(8), target_lang VARCHAR(8),
                    state VARCHAR(32), started_at DATETIME, ended_at DATETIME,
                    report JSON, documents JSON
                )
            """)
            )
            await conn.execute(
                text("""
                INSERT INTO meetings (id, title, state, started_at)
                VALUES ('legacy', 'Eski toplantı', 'ended', '2026-10-01 10:00:00')
            """)
            )
        await db.init_db()
        await db.init_db()
        meeting = await repo.get_meeting("legacy")
        assert meeting.title == "Eski toplantı" and meeting.owner_account_id is None
        account = await repo.set_panel_account("viewer@example.com", "hash", role="viewer")
        assert not await repo.meeting_access("legacy", account.id, "viewer")
        await repo.set_meeting_grant("legacy", account.id, "read")
        assert await repo.meeting_access("legacy", account.id, "viewer")
        from sqlalchemy import delete

        from katip.store.db import session_scope
        from katip.store.models import Meeting

        async with session_scope() as session:
            await session.execute(delete(Meeting).where(Meeting.id == "legacy"))
        assert await repo.list_meeting_grants("legacy") == []
    finally:
        await db.dispose()
