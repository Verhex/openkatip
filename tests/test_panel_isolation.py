"""İki kullanıcı, paylaşılan izleyici ve yöneticinin HTTP/WS erişim sınırları."""

from __future__ import annotations

import asyncio
import time
from functools import partial
from types import SimpleNamespace

import pytest
from conftest import OTHER_TEST_PASSWORD, TEST_PASSWORD
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_route_guards import iter_routes

from katip.api import auth, security
from katip.api.main import app
from katip.api.routes import documents
from katip.config import get_settings
from katip.events import Segment, SessionState
from katip.integrations import mail
from katip.pipeline.session import registry
from katip.store import repo
from katip.workers import jobs

ADMIN = {"Authorization": "Bearer test-admin"}
PASSWORD = TEST_PASSWORD


def login(client, name):
    response = client.post(
        "/api/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KATIP_ADMIN_LOGIN_EMAIL", "env@example.com")
    monkeypatch.setenv("KATIP_ADMIN_PASSWORD_HASH", auth.hash_password(PASSWORD))
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "guard", auth.LoginGuard())
    monkeypatch.setattr(auth, "_FAIL_DELAY_S", 0)

    async def parse(data, kind):
        return [{"location": "bölüm 1", "text": data.decode()}]

    async def draft(sid, access):
        return {"meeting_id": sid, "can_manage": await access.allows(sid, "manage")}

    monkeypatch.setattr(documents.service, "parse_document", parse)
    monkeypatch.setattr(mail, "get_draft", draft)
    with TestClient(app, base_url="https://testserver") as client:
        accounts = {}
        for name, role in (
            ("a", "operator"),
            ("b", "operator"),
            ("c", "viewer"),
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
        live = []
        for sid, owner in (("a-shared", "a"), ("a-private", "a"), ("b-private", "b")):
            client.portal.call(
                partial(
                    repo.upsert_meeting,
                    sid,
                    owner_account_id=accounts[owner],
                    title=sid,
                    state="active",
                    platform="meet",
                )
            )
            files = {}
            for fmt in ("pdf", "docx"):
                path = tmp_path / f"{sid}.{fmt}"
                path.write_bytes(sid.encode())
                files[fmt] = str(path)
            client.portal.call(repo.set_report, sid, {"title": sid}, files)
            client.portal.call(partial(repo.add_usage, sid, stt_seconds=120, llm_usd=1.0))
            client.portal.call(
                repo.save_segment,
                sid,
                Segment(
                    id=sid,
                    session_id=sid,
                    start_s=0,
                    end_s=1,
                    text=sid,
                    lang="tr",
                    is_final=True,
                ),
            )
            client.portal.call(repo.add_asset, sid, "screenshot", 1, files["pdf"], 0, None)
            client.portal.call(
                jobs.enqueue, sid, {"url": "https://meet.google.com/abc-defg-hij"}, 60
            )
            client.post(
                f"/api/preparations/{sid}/documents?filename=private.txt",
                headers=ADMIN,
                content=sid.encode(),
            )
            live.append(
                SimpleNamespace(
                    session_id=sid,
                    state=SessionState.ACTIVE,
                    mode="tam",
                    reply_mode="chat",
                    paused=False,
                    _roster={},
                    final_segments=lambda: [],
                    human_roster=lambda: [],
                    target_lang=None,
                    _created_at=time.monotonic(),
                    presentation_state={"state": "idle"},
                    subscribe=asyncio.Queue,
                    unsubscribe=lambda queue: None,
                )
            )
        client.portal.call(repo.set_meeting_grant, "a-shared", accounts["c"], "read")
        monkeypatch.setattr(registry, "all", lambda: live)
        monkeypatch.setattr(
            registry, "get", lambda sid: next((s for s in live if s.session_id == sid), None)
        )
        yield client, accounts
        monkeypatch.setattr(registry, "all", lambda: [])


# İç bot yüklemeleri ayrı bot anahtarı ister; aşağıdaki envanter tüm panel toplantı uçlarıdır.
SCOPED = [
    (method, context.path)
    for route, context in iter_routes(app.routes)
    if isinstance(route, APIRoute)
    and ("{session_id}" in route.path or "{meeting_id}" in route.path)
    and not route.path.startswith("/api/internal/")
    for method in sorted(context.methods)
]


def request_body(path):
    if path.endswith("/mail/send"):
        return {"draft_id": "a" * 32, "version": 1}
    if path.endswith("/mail"):
        return {"draft_id": "a" * 32, "version": 1, "recipients": []}
    if path.endswith("/presentation"):
        return {"action": "start"}
    if path.endswith("/mode"):
        return {"mode": "chat"}
    if path.endswith("/grants"):
        return {"email": "c@example.com", "permission": "read"}
    if path.endswith("/participants"):
        return {"participants": []}
    if path.endswith("/binding"):
        return {"name": "Ayşe", "email": "a@example.com"}
    if path.endswith("/resolve"):
        return {"confirmation": "worker_stop_verified"}
    return None


def route_path(path, sid):
    return (
        path.replace("{session_id}", sid)
        .replace("{meeting_id}", sid)
        .replace("{fmt}", "pdf")
        .replace("{number}", "1")
        .replace("{document_id}", "missing")
        .replace("{account_id}", "9999")
    )


@pytest.mark.parametrize("method,path", SCOPED)
def test_every_meeting_endpoint_hides_other_users_data(isolated, method, path):
    client, _ = isolated
    headers = login(client, "b")
    response = client.request(
        method, route_path(path, "a-shared"), json=request_body(path), headers=headers
    )
    assert response.status_code == 404, (method, path, response.text)
    assert "a-shared" not in response.text
    headers = login(client, "a")
    response = client.request(
        method, route_path(path, "b-private"), json=request_body(path), headers=headers
    )
    assert response.status_code == 404, (method, path, response.text)


@pytest.mark.parametrize("method,path", [(m, p) for m, p in SCOPED if m != "GET"])
def test_viewer_cannot_write_even_a_granted_meeting(isolated, method, path):
    client, _ = isolated
    headers = login(client, "c")
    response = client.request(
        method, route_path(path, "a-shared"), json=request_body(path), headers=headers
    )
    assert response.status_code == 403, (method, path, response.text)


@pytest.mark.parametrize("method,path", [(m, p) for m, p in SCOPED if m == "GET"])
def test_viewer_reads_only_granted_content_and_admin_can_read_all(isolated, method, path):
    client, _ = isolated
    login(client, "c")
    admin_only = any(word in path for word in ("/debug", "/grants"))
    assert client.get(route_path(path, "a-shared")).status_code == (403 if admin_only else 200)
    assert client.get(route_path(path, "a-private")).status_code in (403, 404)
    assert client.get(route_path(path, "b-private")).status_code in (403, 404)
    for sid in ("a-shared", "a-private", "b-private"):
        response = client.get(route_path(path, sid), headers=ADMIN)
        # Teşhis dosyası olmayan toplantıda yönetici de dosya bulunamadı yanıtı alır.
        assert response.status_code == (404 if "/debug" in path else 200), response.text


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a", {"a-shared", "a-private"}),
        ("b", {"b-private"}),
        ("c", {"a-shared"}),
        ("env", {"a-shared", "a-private", "b-private"}),
    ],
)
def test_lists_and_usage_are_scoped(isolated, name, expected):
    client, _ = isolated
    login(client, name)
    assert {m["id"] for m in client.get("/api/admin/meetings").json()["meetings"]} == expected
    assert {m["session_id"] for m in client.get("/api/meetings").json()} == expected
    assert {m["session_id"] for m in client.get("/api/workers/jobs").json()} == expected
    usage = client.get("/api/usage").json()
    assert {m["meeting_id"] for m in usage["meetings"]} == expected
    assert usage["totals"]["minutes"] == len(expected) * 2
    assert usage["totals"]["usd"]["total"] == len(expected)
    for sid in expected:
        docs = client.get(f"/api/preparations/{sid}/documents").json()
        assert len(docs) == 1 and docs[0]["name"] == "private.txt"


@pytest.mark.parametrize("name", ["a", "b", "c"])
def test_calendar_accounts_and_system_are_admin_only(isolated, name):
    client, accounts = isolated
    headers = login(client, name)
    for method, path, body in (
        ("GET", "/api/integrations/calendar", None),
        ("GET", "/api/integrations/calendar/queue", None),
        ("GET", "/api/integrations/calendar/upcoming", None),
        ("POST", "/api/integrations/calendar/refresh", None),
        ("POST", "/api/integrations/calendar/queue/any/approve", None),
        ("POST", "/api/integrations/calendar/queue/any/reject", None),
        ("GET", "/api/auth/accounts", None),
        ("POST", "/api/auth/accounts", {"email": "new@example.com", "password": PASSWORD}),
        ("PATCH", f"/api/auth/accounts/{accounts['b']}", {"role": "admin"}),
        ("DELETE", f"/api/auth/accounts/{accounts['b']}", None),
        ("GET", "/api/admin/overview", None),
        ("GET", "/api/admin/settings", None),
        ("GET", "/api/people", None),
        ("GET", "/api/approvals", None),
        ("GET", f"/api/admin/meetings?account_id={accounts['b']}", None),
    ):
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, (path, response.text)


def test_patch_csrf_self_and_last_admin_guards(isolated):
    client, accounts = isolated
    headers = login(client, "admin")
    path = f"/api/auth/accounts/{accounts['admin']}"
    assert client.patch(path, json={"role": "operator"}).status_code == 403
    for body in ({"role": "operator"}, {"enabled": False}):
        assert client.patch(path, json=body, headers=headers).status_code == 403
        assert (
            client.post(
                "/api/auth/accounts",
                headers=headers,
                json={
                    "email": "admin@example.com",
                    "password": PASSWORD,
                    "role": "operator",
                },
            ).status_code
            == 403
        )
    # Ortam yöneticisi yokken başka bir yönetici son hesabı kapatamaz.
    env_headers = login(client, "env")
    get_settings().admin_token = ""
    get_settings().admin_password_hash = ""
    assert client.patch(path, json={"enabled": False}, headers=env_headers).status_code == 409
    assert client.patch(path, json={"role": "viewer"}, headers=env_headers).status_code == 409
    assert client.delete(path, headers=env_headers).status_code == 409
    get_settings().admin_token = "test-admin"
    assert client.patch(path, json={"enabled": False}, headers=ADMIN).status_code == 200


def test_disable_and_role_changes_revoke_sessions_and_audit(isolated):
    client, accounts = isolated
    login(client, "b")
    cookie = client.cookies.get(auth.SESSION_COOKIE)
    path = f"/api/auth/accounts/{accounts['b']}"
    assert client.patch(path, json={"enabled": False}, headers=ADMIN).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"email": "b@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert client.patch(path, json={"enabled": True}, headers=ADMIN).status_code == 200
    # Yeniden açmak eski çerezi canlandırmaz.
    assert (
        client.get("/api/auth/me", headers={"Cookie": f"katip_admin={cookie}"}).status_code == 401
    )
    login(client, "b")
    assert client.patch(path, json={"role": "viewer"}, headers=ADMIN).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    login(client, "b")
    assert client.get("/api/auth/me").json()["role"] == "viewer"
    events = client.portal.call(
        repo.recent_audit,
        (
            "panel_account_role_changed",
            "panel_account_disabled",
            "panel_account_enabled",
            "panel_account_created",
        ),
    )
    assert {e.action for e in events} == {
        "panel_account_role_changed",
        "panel_account_disabled",
        "panel_account_enabled",
        "panel_account_created",
    }
    assert all("password" not in str(e.detail) for e in events)


def test_password_change_revokes_all_account_sessions_only(isolated):
    client, _ = isolated
    login(client, "a")
    old_cookie = client.cookies.get(auth.SESSION_COOKIE)
    login(client, "b")
    other_cookie = client.cookies.get(auth.SESSION_COOKIE)
    headers = login(client, "a")
    assert (
        client.post(
            "/api/auth/password",
            headers=headers,
            json={
                "current": PASSWORD,
                "new": OTHER_TEST_PASSWORD,
            },
        ).json()["relogin"]
        is True
    )
    for cookie in (old_cookie, client.cookies.get(auth.SESSION_COOKIE, "gone")):
        assert (
            client.get("/api/auth/me", headers={"Cookie": f"katip_admin={cookie}"}).status_code
            == 401
        )
    assert (
        client.get("/api/auth/me", headers={"Cookie": f"katip_admin={other_cookie}"}).status_code
        == 200
    )


def test_default_role_account_session_statistics_and_live_monitor(isolated):
    client, accounts = isolated
    created = client.post(
        "/api/auth/accounts", headers=ADMIN, json={"email": "new@example.com", "password": PASSWORD}
    ).json()
    assert created["role"] == "operator"
    login(client, "a")
    login(client, "a")
    rows = {a["id"]: a for a in client.get("/api/auth/accounts", headers=ADMIN).json()}
    assert rows[created["id"]]["last_seen"] is None
    assert rows[accounts["a"]]["active_sessions"] == 2
    assert rows[accounts["a"]]["last_seen"]
    assert rows[accounts["a"]]["meetings_30d"] == 2
    assert rows[accounts["a"]]["minutes_30d"] == 4
    assert rows[accounts["a"]]["cost_30d"] == 2
    assert rows[accounts["a"]]["active"] is True
    overview = client.get("/api/admin/overview", headers=ADMIN).json()["sessions"]
    assert len(overview) == 3
    assert overview[0]["owner"] == "a@example.com"
    assert overview[0]["title"] == "a-shared" and overview[0]["cost_usd"] == 1


def test_operator_grants_accept_email_without_account_enumeration(isolated):
    client, accounts = isolated
    headers = login(client, "a")
    path = "/api/meetings/a-shared/grants"
    for body in (
        {"account_id": accounts["b"]},
        {"email": "missing@example.com"},
        {"email": "invalid"},
    ):
        response = client.post(path, headers=headers, json={**body, "permission": "read"})
        assert response.status_code == 404
        assert response.json() == {"detail": "İzin verilecek hesap bulunamadı"}
    assert (
        client.post(
            path, headers=headers, json={"email": "B@EXAMPLE.COM", "permission": "read"}
        ).status_code
        == 200
    )


def test_websocket_scope_and_signed_links(isolated):
    client, _ = isolated
    login(client, "b")
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("wss://testserver/ws/panel/a-shared"):
            pass
    assert error.value.code == 1008
    login(client, "c")
    with client.websocket_connect("wss://testserver/ws/panel/a-shared") as socket:
        assert socket.receive_json()["kind"] == "mode"
    token = security.sign("a-shared", ttl_s=60)
    client.cookies.clear()
    assert client.get(f"/api/meetings/a-shared/transcript?t={token}").status_code == 200
    assert client.get(f"/api/meetings/b-private/transcript?t={token}").status_code == 401


def test_disabled_account_is_rechecked_without_session_deletion(isolated):
    from sqlalchemy import update

    from katip.store.models import PanelAccount

    client, accounts = isolated
    login(client, "a")

    async def disable_directly():
        async with repo.write_scope() as session:
            await session.execute(
                update(PanelAccount).where(PanelAccount.id == accounts["a"]).values(enabled=False)
            )

    client.portal.call(disable_directly)
    assert client.get("/api/admin/meetings").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver/ws/panel/a-shared"):
            pass


def test_hesap_has_admin_security_headers_and_shared_assets(isolated):
    client, _ = isolated
    page = client.get("/hesap")
    assert page.status_code == 200
    assert (
        page.headers["content-security-policy"]
        == client.get("/admin").headers["content-security-policy"]
    )
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/hesap/static/hesap.js").status_code == 200
    for name in ("a", "b", "c"):
        login(client, name)
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/hesap"
        assert client.get("/hesap", follow_redirects=False).status_code == 200
    login(client, "env")
    response = client.get("/hesap", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin"


def test_delete_keeps_existing_disable_contract_and_records_audit(isolated):
    client, accounts = isolated
    path = f"/api/auth/accounts/{accounts['b']}"
    assert client.delete(path, headers=ADMIN).json() == {"disabled": True}
    assert client.portal.call(repo.get_panel_account, accounts["b"]).enabled is False
    events = client.portal.call(
        repo.recent_audit, ("panel_account_deleted", "panel_account_disabled")
    )
    assert {e.action for e in events} == {"panel_account_deleted", "panel_account_disabled"}


def test_meeting_owner_limits_past_reports_even_with_spoofed_participant_email(isolated):
    client, _ = isolated
    reports = client.portal.call(
        partial(
            repo.recent_reports,
            reader_emails={"b@example.com"},
            scope_meeting_id="a-shared",
        )
    )
    assert reports == []
    reports = client.portal.call(
        partial(
            repo.recent_reports,
            reader_emails={"a@example.com", "b@example.com"},
            scope_meeting_id="a-shared",
        )
    )
    assert reports == []
    reports = client.portal.call(
        partial(
            repo.recent_reports,
            reader_emails={"a@example.com"},
            scope_meeting_id="a-shared",
            exclude="a-shared",
        )
    )
    assert [m.id for m in reports] == ["a-private"]
    client.portal.call(repo.upsert_meeting, "ownerless")
    assert (
        client.portal.call(
            partial(
                repo.recent_reports,
                reader_emails={"b@example.com"},
                scope_meeting_id="ownerless",
            )
        )
        == []
    )


def test_role_is_read_from_database_on_every_request(isolated):
    from sqlalchemy import update

    from katip.store.models import PanelAccount

    client, accounts = isolated
    headers = login(client, "a")

    async def change_role(role):
        async with repo.write_scope() as session:
            await session.execute(
                update(PanelAccount)
                .where(
                    PanelAccount.id == accounts["a"],
                )
                .values(role=role)
            )

    assert client.post("/api/meetings/a-shared/share", headers=headers).status_code == 200
    client.portal.call(change_role, "viewer")
    assert client.get("/api/auth/me").json()["role"] == "viewer"
    assert client.get("/api/meetings/a-shared/report").status_code == 404
    assert client.post("/api/meetings/a-shared/share", headers=headers).status_code == 403
    client.portal.call(change_role, "admin")
    assert client.get("/api/admin/overview").status_code == 200
