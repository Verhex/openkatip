"""E-posta eşleştirmesi, panel kimliği ve Graph için davranış regresyonları."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from test_admin import PASSWORD, login
from test_admin import client as admin_client_fixture

from katip.agent import controller
from katip.api import auth
from katip.config import get_settings
from katip.events import RosterUpdate, Segment
from katip.identity import ParticipantIdentity, company_email, normalize_email
from katip.integrations.microsoft import MicrosoftGraph, sync_identities
from katip.pipeline.session import MeetingSession
from katip.reports.service import _participants
from katip.store import db, repo
from katip.store.org import OrgChart, apply_org_chart

client = admin_client_fixture


class SilentSTT:
    translates = True

    async def stream(self, frames, **kwargs):
        if False:
            yield


def make_session():
    return MeetingSession("emails", stt=SilentSTT())


def segment(name="Ayşe", email=None):
    return Segment(
        id="s1",
        session_id="emails",
        speaker=name,
        speaker_label="1",
        speaker_email=email,
        start_s=0,
        end_s=3,
        text="Bir karar aldık.",
        lang="tr",
        is_final=True,
    )


async def test_email_list_binds_only_observed_people_and_preserves_translation(database):
    session = make_session()
    await session.add_identities(
        [
            ParticipantIdentity(name="Ayşe", email="AYSE@EXAMPLE.COM"),
            ParticipantIdentity(name="Davetli", email="davetli@example.com"),
        ],
        "calendar",
    )
    assert await repo.list_participants("emails") == []
    session._participants["Ayşe"] = None
    session.segments["s1"] = segment()
    await session._register_participants(["Ayşe"])
    assert session.segments["s1"].speaker_email == "ayse@example.com"
    assert session._internal["Ayşe"] is False  # davet/isim eşleştirmesi geçmiş raporları açmaz
    await session.shutdown()
    stored = await repo.get_meeting("emails")
    assert stored.segments[0].speaker_email == "ayse@example.com"
    assert stored.segments[0].person_id is not None
    assert [p.name for p in await repo.list_participants("emails")] == ["Ayşe"]


async def test_org_name_alone_does_not_supply_email_or_internal_access(database):
    await apply_org_chart(
        OrgChart.from_yaml("people:\n  - name: Ayşe\n    email: ayse@example.com\n")
    )
    session = make_session()
    session._roster = ["Ayşe"]
    session._participants["Ayşe"] = None
    await session._register_participants(["Ayşe"])
    assert not session.participants_all_internal()
    assert session._participants["Ayşe"] is None
    await session.shutdown()


async def test_duplicate_names_remain_unbound_until_panel_selection(database):
    session = make_session()
    session._participants["Ali"] = None
    await session.add_identities(
        [
            ParticipantIdentity(name="Ali", email="ali1@example.com"),
            ParticipantIdentity(name="Ali", email="ali2@example.com"),
        ],
        "calendar",
    )
    assert session._participants["Ali"] is None
    assert len(await repo.list_meeting_identities("emails")) == 2
    chosen = ParticipantIdentity(name="Ali", email="ali2@example.com")
    await session.bind_identity("Ali", chosen)
    await session.add_identities(
        [ParticipantIdentity(name="Ali", email="ali3@example.com")], "calendar"
    )
    assert session._participants["Ali"] == "ali2@example.com"
    await session.shutdown()


async def test_same_name_different_email_has_distinct_person(database):
    a = await repo.ensure_person_by_email("ali1@example.com", "Ali")
    b = await repo.ensure_person_by_email("ali2@example.com", "Ali")
    assert a.id != b.id
    assert (await repo.ensure_person_by_email("ALI1@EXAMPLE.COM", "Ali Yeni")).id == a.id


async def test_report_uses_saved_meeting_email_not_name_lookup(database):
    await apply_org_chart(
        OrgChart.from_yaml("people:\n  - name: Ayşe\n    email: wrong@example.com\n")
    )
    await repo.upsert_participant("emails", "Ayşe", None, "actual@example.com")
    assert await _participants("emails", []) == [("Ayşe", None, "actual@example.com")]


async def test_existing_sqlite_schema_migrates_without_losing_rows(database):
    await repo.upsert_participant("emails", "Ayşe", None, "ayse@example.com")
    engine = db._engine
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE meeting_participants DROP COLUMN platform_id"))
        await conn.execute(text("ALTER TABLE meeting_participants DROP COLUMN identity_source"))
        await conn.execute(text("ALTER TABLE meeting_segments DROP COLUMN speaker_email"))
        # SQLite ayrı FK tanımı olan sütunu DROP COLUMN ile silemez;
        # eski sürümdeki oturum tablosunu ve bir oturumu doğrudan oluştur.
        await conn.execute(text("DROP TABLE admin_sessions"))
        await conn.execute(
            text("""
            CREATE TABLE admin_sessions (
                id INTEGER PRIMARY KEY, token_hash VARCHAR(64) UNIQUE,
                csrf_hash VARCHAR(64), created_at DATETIME, last_seen DATETIME,
                expires_at DATETIME, client VARCHAR(64), user_agent VARCHAR(200)
            )
        """)
        )
        await conn.execute(
            text("""
            INSERT INTO admin_sessions
                (id, token_hash, csrf_hash, created_at, last_seen, expires_at)
            VALUES (1, 'legacy-token', 'legacy-csrf', '2026-10-05 12:00:00',
                    '2026-10-05 12:00:00', '2026-10-06 12:00:00')
        """)
        )
    await db.init_db()
    await db.init_db()
    rows = await repo.list_participants("emails")
    assert rows[0].email == "ayse@example.com" and rows[0].identity_source == "unknown"
    legacy = await repo.get_admin_session("legacy-token")
    assert legacy.account_id is None and legacy.csrf_hash == "legacy-csrf"


async def test_disabling_voice_does_not_disable_roster_or_commands(database, monkeypatch):
    session = make_session()
    assert session._voice_enabled is False and session.identifier is None

    async def forbidden():
        raise AssertionError("Ses kimliği yüklenmemeli")

    monkeypatch.setattr(session, "_init_voice", forbidden)
    session.push_audio(b"\0\0" * 1024)
    assert session._voice_ready is None
    session.update_roster(RosterUpdate(participants=["Ayşe"]))
    await session.agent.on_utterance("1", "Katip, sesimi tanı")
    assert session.identifier is None
    await session.shutdown()


def create_account(client, email="ayse@example.com", password=PASSWORD, role="operator"):
    return client.post(
        "/api/auth/accounts",
        json={"email": email, "password": password, "role": role},
        headers={"Authorization": "Bearer test-admin"},
    )


def test_panel_account_requires_provisioned_company_email_and_password(client):
    assert (
        client.post(
            "/api/auth/accounts", json={"email": "ayse@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert create_account(client, "ayse@other.com").status_code == 422
    assert create_account(client, "ayse@example.com.evil.com").status_code == 422
    assert create_account(client, "ayse@example.com", "aaaaaaaaaaaaaaaa").status_code == 422
    assert create_account(client).status_code == 200
    assert create_account(client, "ayse@example.net").status_code == 200
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"email": "unknown@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": "AYSE@EXAMPLE.COM", "password": PASSWORD}
        ).status_code
        == 200
    )
    assert client.get("/api/auth/me").json()["email"] == "ayse@example.com"


def test_reset_or_disable_account_revokes_existing_sessions(client):
    account = create_account(client).json()
    r = client.post("/api/auth/login", json={"email": account["email"], "password": PASSWORD})
    assert r.status_code == 200
    assert create_account(client, password="Different-Pass-2026!").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"email": account["email"], "password": "Different-Pass-2026!"}
        ).status_code
        == 200
    )
    r = client.delete(
        f"/api/auth/accounts/{account['id']}", headers={"X-CSRF-Token": auth._b64(b"wrong")}
    )
    assert r.status_code == 403
    # Betik yöneticisi hesabı kapatır; mevcut oturum da anında geçersizdir.
    client.cookies.clear()
    assert (
        client.delete(
            f"/api/auth/accounts/{account['id']}", headers={"Authorization": "Bearer test-admin"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": account["email"], "password": "Different-Pass-2026!"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("email", ["ayse@ſample.example", "ayse@examKle.com"])
def test_login_cannot_casefold_unicode_domain_into_company_account(client, email):
    get_settings().panel_email_domains.extend(["examkle.com", "sample.example"])
    ascii_email = email.casefold()
    assert create_account(client, ascii_email).status_code == 200
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 401
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("özgür@example.com", "özgür@example.com"),
        ("\u00a0ÖZGÜR@EXAMPLE.COM\u00a0", "özgür@example.com"),
        ("\u00a0AYSE@EXAMPLE.COM\u00a0", "ayse@example.com"),
        ("K@EXAMPLE.COM", "k@example.com"),
        ("ſtraße@EXAMPLE.COM", "strasse@example.com"),
    ],
)
def test_email_normalization_preserves_main_local_part_and_whitespace(raw, expected):
    assert normalize_email(raw) == expected
    assert normalize_email(expected) == expected
    assert ParticipantIdentity(name="Özgür", email=raw).email == expected
    assert company_email(raw, ["example.com"])


@pytest.mark.parametrize("email", ["özgür@example.com", "\u00a0ÖZGÜR@EXAMPLE.COM\u00a0"])
def test_existing_turkish_panel_account_can_log_in(client, email):
    account = client.portal.call(
        repo.set_panel_account, "özgür@example.com", auth.hash_password(PASSWORD)
    )
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert client.get("/api/auth/me").json()["email"] == account.email


def test_panel_provisioning_normalizes_turkish_address_consistently(client):
    response = create_account(client, "\u00a0ÖZGÜR@EXAMPLE.COM\u00a0")
    assert response.status_code == 200, response.text
    assert response.json()["email"] == "özgür@example.com"
    response = client.post(
        "/api/auth/login", json={"email": "ÖZGÜR@EXAMPLE.COM", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_binding_routes_require_login_and_csrf(client):
    assert client.get("/api/meetings/missing/participants").status_code == 401
    csrf = login(client)
    assert client.get("/api/meetings/missing/participants").status_code == 404
    assert (
        client.put(
            "/api/meetings/missing/participants/binding",
            json={"name": "Ayşe", "email": "ayse@example.com"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/meetings/missing/participants/binding",
            json={"name": "Ayşe", "email": "bad"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )


async def test_graph_uses_pagination_and_skips_cancelled_invitation(database, monkeypatch):
    s = get_settings()
    s.m365_tenant_id, s.m365_client_id, s.m365_client_secret = "tenant", "client", "secret"
    url = "https://teams.microsoft.com/meet/123?p=abc"
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert b"client_secret=secret" in request.content
            return httpx.Response(200, json={"access_token": "token"})
        assert request.headers["Authorization"] == "Bearer token"
        event = {
            "start": {"dateTime": "2026-10-05T12:00:00Z"},
            "onlineMeeting": {"joinUrl": url},
            "organizer": {"emailAddress": {"name": "Ayşe", "address": "AYSE@EXAMPLE.COM"}},
            "attendees": [
                {"emailAddress": {"name": "Katip", "address": "katip@example.com"}},
                {"emailAddress": {"name": "Bozuk", "address": "invalid"}},
            ],
        }
        if request.url.path.endswith("calendarView"):
            return httpx.Response(
                200,
                json={
                    "value": [dict(event, isCancelled=True)],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
                },
            )
        return httpx.Response(200, json={"value": [event]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identities = await MicrosoftGraph(client).invitation(
            url, datetime(2026, 10, 5, 12, tzinfo=UTC)
        )
    assert len(requests) == 3
    assert identities == [ParticipantIdentity(name="Ayşe", email="ayse@example.com")]


async def test_graph_does_not_send_token_to_foreign_pagination_host(database):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.test/steal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await MicrosoftGraph(client).collection("/users/test/calendarView")
    assert len(calls) == 2


async def test_unconfigured_graph_makes_no_network_request(database):
    assert await sync_identities("emails") == {"configured": False, "imported": 0}


async def test_meeting_emails_and_guest_suffix_bind_without_voice(database):
    session = make_session()
    await session.add_identities(
        [ParticipantIdentity(name="Ayşe", email="ayse@example.net")], "meeting"
    )
    session.update_roster(
        RosterUpdate(
            participants=["Ayşe (Guest)", "Katip (Guest)"],
            active_speakers=["Katip (Guest)"],
        )
    )
    await session._register_participants(["Ayşe (Guest)"])
    assert session._roster == ["Ayşe (Guest)"]
    # Browser roster claims can map an email, but cannot grant access to past reports.
    assert not session.participants_all_internal()
    assert session._participants["Ayşe (Guest)"] == "ayse@example.net"
    await session.shutdown()


async def test_org_import_does_not_merge_people_with_same_name(database):
    a = await repo.ensure_person_by_email("ali1@example.com", "Ali")
    b = await repo.ensure_person_by_email("ali2@example.com", "Ali")
    await apply_org_chart(
        OrgChart.from_yaml(
            "people:\n  - name: Ali\n    email: ali1@example.com\n    title: Yönetici\n"
        )
    )
    assert (await repo.get_person(a.id)).title == "Yönetici"
    assert (await repo.get_person(b.id)).email == "ali2@example.com"


async def test_forgetting_one_email_does_not_erase_another_same_named_person(database):
    a = await repo.ensure_person_by_email("ali1@example.com", "Ali")
    b = await repo.ensure_person_by_email("ali2@example.com", "Ali")
    await repo.save_segment("emails", segment("Ali", b.email), b.id)
    await repo.upsert_participant("emails", "Ali", b.id, b.email)
    assert await repo.forget_person(a.id, requested_by="panel")
    meeting = await repo.get_meeting("emails")
    assert meeting.segments[0].person_id == b.id
    assert meeting.segments[0].speaker_email == b.email
    assert (await repo.list_participants("emails"))[0].email == b.email


def test_company_account_can_approve_only_with_session_and_csrf(client):
    class ProposalHandler:
        async def apply_approval(self, approval):
            await repo.add_directive(approval.directive, approval.session_id)

    create_account(client, "reviewer@example.net", role="admin")
    approval = controller.Approval(
        id="email-check",
        session_id="emails",
        description="Rapor dili",
        directive="Türkçe rapor",
        agent=ProposalHandler(),
    )
    controller.APPROVALS[approval.id] = approval
    try:
        path = "/api/approvals/email-check/approve"
        assert client.post(path).status_code == 401
        result = client.post(
            "/api/auth/login",
            json={"email": "reviewer@example.net", "password": PASSWORD},
        )
        assert result.status_code == 200
        assert client.post(path).status_code == 403
        assert client.portal.call(repo.list_directives) == []
        assert client.post(path, headers={"X-CSRF-Token": result.json()["csrf"]}).status_code == 200
        assert client.portal.call(repo.list_directives) == ["Türkçe rapor"]
        audit = client.portal.call(repo.recent_audit, ("directive_approved",))
        assert audit[0].detail["account_id"] is not None
    finally:
        controller.APPROVALS.pop(approval.id, None)


def test_panel_binding_updates_stored_transcript_and_invalidates_old_documents(client):
    async def seed():
        await repo.upsert_meeting("emails", documents={"pdf": "old.pdf"})
        await repo.save_segment("emails", segment())
        await repo.upsert_participant("emails", "Ayşe", None, None)

    client.portal.call(seed)
    csrf = login(client)
    path = "/api/meetings/emails/participants/binding"
    body = {"name": "Ayşe", "email": "ayse@example.com"}
    assert client.put(path, json=body).status_code == 403
    assert (
        client.put(
            path,
            json={"name": "Toplantıda yok", "email": "other@example.com"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )
    assert client.put(path, json=body, headers={"X-CSRF-Token": csrf}).status_code == 200
    stored = client.portal.call(repo.get_meeting, "emails")
    assert stored.segments[0].speaker_email == "ayse@example.com"
    assert stored.segments[0].person_id is not None
    assert stored.documents == {}
