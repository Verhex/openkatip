"""Onaylı mail, Graph yanıtları, erişim ve şifreli taslak yaşam döngüsü."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from conftest import TEST_PASSWORD
from fastapi import HTTPException
from sqlalchemy import select, text

from katip.api.main import app
from katip.api.security import PanelAccess
from katip.config import Settings, get_settings
from katip.identity import ParticipantIdentity, company_email
from katip.integrations import mail
from katip.integrations.microsoft import MicrosoftGraph
from katip.store import crypto, repo
from katip.store.db import session_scope
from katip.store.models import AuditLog, MeetingMailDraft

REPORT = {
    "title": "Bütçe",
    "summary": '<script>alert("ğüşıİöç")</script> https://evil.example <a href="evil">link</a>',
    "key_points": [],
    "decisions": [{"text": "Bütçe onaylandı <img src=x>", "owner": "Ayşe"}],
    "action_items": [{"task": "Teklifi hazırla", "owner": "Ayşe", "due": "15 Ekim"}],
    "open_questions": [],
    "participants": [],
}
ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def enabled():
    s = get_settings()
    s.mail_enabled = True
    s.m365_tenant_id, s.m365_client_id, s.m365_client_secret = "tenant", "client", "secret"
    s.public_base_url = "https://katip.example"
    return s


async def seed(mid="m1", files=None):
    owner = await repo.set_panel_account("owner@example.com", "unused", role="operator")
    await repo.upsert_meeting(
        mid, title="Bütçe", started_at=datetime(2026, 10, 9, tzinfo=UTC), owner_account_id=owner.id
    )
    await repo.set_report(mid, REPORT, files or {})
    for name, email, source in (
        ("Ayşe", "AYSE@EXAMPLE.COM", "calendar"),
        ("Konuk", "guest@external.example", "meeting"),
        ("Eski", "old@example.com", "unknown"),
    ):
        await repo.save_meeting_identity(mid, ParticipantIdentity(name=name, email=email), source)
    return owner


@pytest.fixture
def graph(monkeypatch):
    calls = []
    responses = [202]
    real_client = httpx.AsyncClient

    async def handler(request):
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "fake"})
        calls.append(request)
        await asyncio.sleep(0)
        status = responses.pop(0) if len(responses) > 1 else responses[0]
        if status == "timeout":
            raise httpx.ReadTimeout("adresler asla loglanmaz", request=request)
        if status == "connect":
            raise httpx.ConnectError("bağlantı kurulamadı", request=request)
        return httpx.Response(status, headers={"request-id": "graph-request", "Retry-After": "0"})

    transport = httpx.MockTransport(handler)

    def client(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(mail.httpx, "AsyncClient", client)
    return calls, responses, real_client


async def test_draft_escaping_candidates_and_owner(database, enabled):
    await seed()
    draft = await mail.get_draft("m1", PanelAccess())
    assert draft["subject"] == "Toplantı notu: Bütçe — 09.10.2026"
    assert set(draft["recipients"]) == {"ayse@example.com", "owner@example.com"}
    assert "guest@external.example" not in draft["recipients"]
    assert "old@example.com" not in {c["email"] for c in draft["candidates"]}
    assert "<script>" not in draft["body_html"]
    assert "&lt;script&gt;" in draft["body_html"]
    assert "<a " not in draft["body_html"]
    assert '<a href="evil">' in draft["body_text"]
    assert "Ayşe; Termin: 15 Ekim" in draft["body_text"]
    assert "https://katip.example/panel/m1" in draft["body_text"]


@pytest.mark.parametrize(
    "value,internal",
    [
        ("A@EXAMPLE.COM", True),
        ("a@example.com.", False),
        ("a@examKle.com", False),
        ("a@example.com.evil", False),
        ("a@external.example", False),
    ],
)
def test_domain_tricks(value, internal):
    assert company_email(value, mail.internal_domains()) is internal
    if not internal and value in {"a@example.com.", "a@examKle.com"}:
        with pytest.raises(ValueError):
            mail.address(value)


def test_comma_domains(monkeypatch):
    monkeypatch.setenv("KATIP_MAIL_INTERNAL_DOMAINS", "EXAMPLE.COM, example.org")
    assert Settings(_env_file=None).mail_internal_domains == "example.com,example.org"
    monkeypatch.setenv("KATIP_MAIL_INTERNAL_DOMAINS", "examKle.com")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


async def test_selection_validation_and_versions(database, enabled):
    await seed()
    await mail.get_draft("m1", PanelAccess())
    for recipients in (["new@external.example"], [f"p{i}@example.com" for i in range(51)]):
        with pytest.raises(HTTPException) as exc:
            await mail.edit("m1", 1, recipients, "")
        assert exc.value.status_code == 422
    draft = await mail.edit(
        "m1",
        1,
        ["NEW@EXAMPLE.COM", "new@example.com", "guest@external.example"],
        "İyi günler <script>",
    )
    assert draft["recipients"] == ["new@example.com", "guest@external.example"]
    assert draft["version"] == 2
    assert "&lt;script&gt;" in draft["body_html"]
    with pytest.raises(HTTPException) as exc:
        await mail.edit("m1", 1, [], "")
    assert exc.value.status_code == 409


async def test_concurrent_posts_send_once_and_lock_edits(database, enabled, graph):
    owner = await seed()
    initial = await mail.get_draft("m1", PanelAccess())
    calls, _, real = graph
    async with real(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        results = await asyncio.gather(
            *[
                client.post(
                    "/api/meetings/m1/mail/send",
                    json={"draft_id": initial["draft_id"], "version": 1},
                    headers=ADMIN,
                )
                for _ in range(2)
            ]
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(calls) == 1
    assert str(calls[0].url).endswith("/users/katip%40example.com/sendMail")
    msg = BytesParser(policy=policy.default).parsebytes(base64.b64decode(calls[0].content))
    assert msg.get_body(preferencelist=("plain",))
    assert msg.get_body(preferencelist=("html",))
    assert "guest@external.example" not in str(msg["To"])
    assert "Katip tarafından hazırlandı" in msg.get_body(preferencelist=("plain",)).get_content()
    draft = await mail.get_draft("m1", PanelAccess(owner.id, "operator"))
    assert draft["status"] == "gönderildi" and draft["sent_at"]
    assert draft["request_id"] == "graph-request"
    with pytest.raises(HTTPException):
        await mail.edit("m1", 1, [], "")
    async with session_scope() as session:
        audit = list(await session.scalars(select(AuditLog)))
    assert "ayse@example.com" not in str([a.detail for a in audit])


@pytest.mark.parametrize("status", [401, 403])
async def test_permission_error_retryable_and_health(database, enabled, graph, status):
    owner = await seed()
    await mail.get_draft("m1", PanelAccess())
    calls, responses, _ = graph
    responses[:] = [status]
    with pytest.raises(HTTPException) as exc:
        await mail.send("m1", 1, PanelAccess(owner.id, "operator"))
    assert exc.value.detail == mail.PERMISSION_ERROR
    draft = await mail.get_draft("m1", PanelAccess())
    assert draft["status"] == "hata"
    assert await mail.health() == (False, "izin hatası son denemede")
    responses[:] = [202]
    assert (await mail.send("m1", 1, PanelAccess()))["status"] == "gönderildi"
    assert len(calls) == 2
    assert await mail.health() == (True, "hazır")


@pytest.mark.parametrize("statuses,count,success", [([429, 202], 2, True), ([429], 3, False)])
async def test_bounded_429_retry_after(database, enabled, graph, statuses, count, success):
    await seed()
    await mail.get_draft("m1", PanelAccess())
    calls, responses, _ = graph
    responses[:] = statuses
    if success:
        assert (await mail.send("m1", 1, PanelAccess()))["status"] == "gönderildi"
    else:
        with pytest.raises(HTTPException):
            await mail.send("m1", 1, PanelAccess())
        assert (await mail.get_draft("m1", PanelAccess()))["status"] == "hata"
    assert len(calls) == count
    assert len({r.headers["client-request-id"] for r in calls}) == 1


@pytest.mark.parametrize("size,attached", [(100, True), (1_900_000, False), (3_000_001, False)])
async def test_pdf_request_size_fallback(database, enabled, graph, tmp_path, size, attached):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"x" * size)
    await seed(files={"pdf": str(path)})
    await mail.get_draft("m1", PanelAccess())
    await mail.send("m1", 1, PanelAccess())
    calls, _, _ = graph
    msg = BytesParser(policy=policy.default).parsebytes(base64.b64decode(calls[0].content))
    assert bool(list(msg.iter_attachments())) is attached
    assert len(calls[0].content) < mail.REQUEST_LIMIT
    assert "https://katip.example/panel/m1" in msg.get_body(preferencelist=("plain",)).get_content()


@pytest.mark.parametrize("mode", ["disabled", "unconfigured"])
async def test_disabled_preview_send_refused(database, enabled, graph, mode):
    await seed()
    if mode == "disabled":
        enabled.mail_enabled = False
    else:
        enabled.m365_client_secret = ""
    draft = await mail.get_draft("m1", PanelAccess())
    assert draft["body_text"] and draft["disabled_reason"]
    with pytest.raises(HTTPException) as exc:
        await mail.send("m1", 1, PanelAccess())
    assert exc.value.status_code == 409
    assert not graph[0]
    assert (await mail.health())[1].startswith("kapalı")


async def test_uncertain_send_never_retries(database, enabled, graph):
    await seed()
    await mail.get_draft("m1", PanelAccess())
    graph[1][:] = ["timeout"]
    with pytest.raises(HTTPException) as exc:
        await mail.send("m1", 1, PanelAccess())
    assert exc.value.detail == mail.UNCERTAIN
    with pytest.raises(HTTPException) as exc:
        await mail.send("m1", 1, PanelAccess())
    assert exc.value.status_code == 409
    assert len(graph[0]) == 1


async def test_connect_error_is_not_sent_and_can_be_approved_again(database, enabled, graph):
    """Bağlantı kurulamadıysa mail gitmemiştir; taslak kilitlenmez, yeniden onaylanır."""
    await seed()
    await mail.get_draft("m1", PanelAccess())
    graph[1][:] = ["connect", 202]
    with pytest.raises(HTTPException) as exc:
        await mail.send("m1", 1, PanelAccess())
    assert exc.value.status_code == 502 and "bağlanılamadı" in exc.value.detail
    assert (await mail.get_draft("m1", PanelAccess()))["status"] == "hata"
    assert (await mail.send("m1", 1, PanelAccess()))["status"] == "gönderildi"
    assert len(graph[0]) == 2


def test_acl_and_request_limits(api_client, enabled):
    from functools import partial

    client = api_client
    client.base_url = "https://testserver"
    owner = client.portal.call(seed)
    accounts = {}
    for role in ("viewer", "operator"):
        response = client.post(
            "/api/auth/accounts",
            headers=ADMIN,
            json={
                "email": f"{role}@example.com",
                "password": TEST_PASSWORD,
                "role": role,
            },
        )
        assert response.status_code == 200
        accounts[role] = response.json()["id"]
    client.portal.call(repo.set_meeting_grant, "m1", accounts["viewer"], "read")
    for role, status in (("viewer", 403), ("operator", 404)):
        login = client.post(
            "/api/auth/login",
            json={
                "email": f"{role}@example.com",
                "password": TEST_PASSWORD,
            },
        )
        headers = {"X-CSRF-Token": login.json()["csrf"]}
        assert (
            client.post(
                "/api/meetings/m1/mail/send", headers=headers, json={"version": 1}
            ).status_code
            == status
        )
        assert (
            client.put(
                "/api/meetings/m1/mail", headers=headers, json={"version": 1, "recipients": []}
            ).status_code
            == status
        )
        preview = client.get("/api/meetings/m1/mail")
        if role == "viewer":
            assert preview.status_code == 200
            assert preview.json()["can_manage"] is False
            assert "recipients" not in preview.json()
        else:
            assert preview.status_code == 404
    assert owner.id
    for payload in (
        {"recipients": [f"p{i}@example.com" for i in range(51)]},
        {"recipients": [], "intro": "x" * 1001},
    ):
        assert (
            client.put(
                "/api/meetings/m1/mail", headers=ADMIN, json={"version": 1, **payload}
            ).status_code
            == 422
        )
    client.portal.call(partial(repo.set_meeting_grant, "m1", accounts["operator"], "manage"))
    assert client.get("/api/meetings/m1/mail").json()["can_manage"] is True


@pytest.fixture
def encrypted():
    get_settings().data_key = base64.b64encode(os.urandom(32)).decode()


async def test_encrypted_draft_updates_and_delete(encrypted, database, enabled):
    await seed()
    await mail.get_draft("m1", PanelAccess())
    await mail.edit("m1", 1, ["extra@example.com"], "Gizli giriş")
    async with session_scope() as session:
        raw = (
            await session.execute(
                text("SELECT payload, encrypted_content FROM meeting_mail_drafts")
            )
        ).one()
        assert "Gizli giriş" not in str(raw)
        assert "extra@example.com" not in str(raw)
        assert raw.encrypted_content
    draft = await mail.get_draft("m1", PanelAccess())
    assert draft["intro"] == "Gizli giriş"
    assert await repo.delete_meeting("m1")
    async with session_scope() as session:
        assert await session.scalar(select(MeetingMailDraft.id)) is None
    with pytest.raises(crypto.DataKeyError):
        await repo.upsert_meeting("m1")


async def test_forget_removes_drafts(encrypted, database, enabled):
    await seed()
    person = await repo.ensure_person_by_email("ayse@example.com", "Ayşe")
    await mail.get_draft("m1", PanelAccess())
    assert await repo.forget_person(person.id, requested_by="panel")
    async with session_scope() as session:
        assert await session.scalar(select(MeetingMailDraft.id)) is None
    draft = await mail.get_draft("m1", PanelAccess())
    assert "ayse@example.com" not in draft["recipients"]
    assert "Ayşe" not in draft["body_text"]


async def test_old_approval_cannot_send_recreated_draft(database, enabled, graph):
    await seed()
    old = await mail.get_draft("m1", PanelAccess())
    person = await repo.ensure_person_by_email("ayse@example.com", "Ayşe")
    assert await repo.forget_person(person.id, requested_by="panel")
    new = await mail.get_draft("m1", PanelAccess())
    assert old["draft_id"] != new["draft_id"]
    assert old["version"] == new["version"] == 1
    with pytest.raises(HTTPException) as exc:
        await mail.send("m1", 1, PanelAccess(), draft_id=old["draft_id"])
    assert exc.value.status_code == 409
    assert not graph[0]


@pytest.mark.parametrize("retry_after,expected", [("2", 2), ("60", None), ("bozuk", None)])
async def test_retry_after_wait_and_upper_bound(enabled, monkeypatch, retry_after, expected):
    calls, waits = [], []

    def handler(request):
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "fake"})
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 202, headers={"Retry-After": retry_after})

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("katip.integrations.microsoft.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await MicrosoftGraph(client).send_mail(b"mail", "request")
    assert response.status_code == (202 if expected is not None else 429)
    assert waits == ([expected] if expected is not None else [])
    assert len(calls) == (2 if expected is not None else 1)


async def test_encrypted_send_and_erasure_after_uncertain_result(
    encrypted, database, enabled, graph
):
    await seed()
    await mail.get_draft("m1", PanelAccess())
    graph[1][:] = ["timeout"]
    with pytest.raises(HTTPException):
        await mail.send("m1", 1, PanelAccess())
    assert await repo.delete_meeting("m1")
    async with session_scope() as session:
        assert await session.scalar(select(MeetingMailDraft.id)) is None
