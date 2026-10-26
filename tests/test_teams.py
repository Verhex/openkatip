from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import struct
import time
import zipfile
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from katip.agent.commands import Intent, Level
from katip.agent.controller import MeetingAgent
from katip.api.main import app
from katip.api.security import PanelAccess
from katip.config import get_settings
from katip.integrations import mail
from katip.store import repo
from katip.store.db import session_scope
from katip.store.models import TeamsConversation
from katip.teams import cards, security, service, store
from katip.teams.activity import Activity
from katip.teams.client import Connector, connector
from katip.teams.config import credentials
from katip.teams.package import build_package, manifest
from katip.teams.security import AuthenticationError, Validator

APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
AAD = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
THREAD = "19:meeting_YWJjZA==@thread.v2"
URL = "https://teams.microsoft.com/l/meetup-join/" + quote(THREAD, safe="") + "/0?context=x"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
REPORT = {
    "title": "Bütçe",
    "date": "2026-10-09",
    "summary": "Gizli özet",
    "topics": [],
    "decisions": [],
    "action_items": [],
    "open_questions": [],
    "key_points": [],
    "participants": [],
}


@pytest.fixture
def configured():
    settings = get_settings()
    settings.teams_bot_app_id = APP_ID
    settings.teams_bot_secret = "teams-secret-not-for-package"
    settings.teams_bot_tenant_id = TENANT
    settings.public_base_url = "https://katip.example"
    return settings


@pytest.fixture
def encrypted(configured):
    configured.data_key = base64.b64encode(os.urandom(32)).decode()
    return configured


def payload(*, personal=False, text="yardım", mention=False, kind="message"):
    if mention:
        text = "<at>Katip</at> " + text
    return {
        "type": kind,
        "id": "activity-1",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "conversation": {
            "id": "personal-chat" if personal else THREAD,
            "conversationType": "personal" if personal else "groupChat",
            "tenantId": TENANT,
        },
        "from": {"id": "29:user", "aadObjectId": AAD, "name": "Asla loglanmaz"},
        "recipient": {"id": "28:" + APP_ID},
        "channelData": {"tenant": {"id": TENANT}},
        "text": text,
        "entities": (
            [{"type": "mention", "mentioned": {"id": "28:" + APP_ID}, "text": "<at>Katip</at>"}]
            if mention
            else []
        ),
    }


@pytest.fixture(scope="module")
def signing_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="known", endorsements=["msteams"])
    return key, jwk


def authorization(signing_key, changes=None, *, kid="known", algorithm="RS256"):
    now = int(time.time())
    claims = {
        "iss": security.ISSUER,
        "aud": APP_ID,
        "exp": now + 300,
        "nbf": now - 5,
        # Gerçek Bot Connector token'ında alan adı küçük harfle gelir.
        "serviceurl": SERVICE_URL,
    }
    claims.update(changes or {})
    key = (
        signing_key[0]
        if algorithm == "RS256"
        else ("confusion" * 8 if algorithm == "HS256" else "")
    )
    return "Bearer " + jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def cached_validator(signing_key):
    validator = Validator()
    validator.keys = {"known": signing_key[1]}
    validator.expires = time.monotonic() + 3600
    validator.refreshed = time.monotonic() - 10
    return validator


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"aud": OTHER},
        {"iss": "https://login.microsoftonline.com/" + TENANT + "/v2.0"},
        {"exp": 1},
        {"nbf": int(time.time()) + 600},
        {"tid": OTHER},
        {"serviceurl": "https://smba.trafficmanager.net/other/"},
        {"serviceurl": None},
        {"aud": [APP_ID, OTHER]},
    ],
)
async def test_jwt_matrix(configured, signing_key, changes):
    validator = cached_validator(signing_key)
    if not changes:
        await validator.validate(
            authorization(signing_key), Activity.model_validate(payload()), credentials()
        )
    else:
        with pytest.raises(AuthenticationError):
            await validator.validate(
                authorization(signing_key, changes),
                Activity.model_validate(payload()),
                credentials(),
            )


async def test_service_url_claim_spelling_from_docs_is_accepted(configured, signing_key):
    """Belgedeki "serviceUrl" yazımı da kabul; alan yoksa reddedilir."""
    activity = Activity.model_validate(payload())
    docs_style = authorization(signing_key, {"serviceurl": None, "serviceUrl": SERVICE_URL})
    await cached_validator(signing_key).validate(docs_style, activity, credentials())
    missing = jwt.decode(authorization(signing_key).split()[1], options={"verify_signature": False})
    missing.pop("serviceurl")
    token = jwt.encode(missing, signing_key[0], algorithm="RS256", headers={"kid": "known"})
    with pytest.raises(AuthenticationError):
        await cached_validator(signing_key).validate("Bearer " + token, activity, credentials())


@pytest.mark.parametrize("part", ["channel", "conversation"])
async def test_activity_other_tenant(configured, signing_key, part):
    data = payload()
    if part == "channel":
        data["channelData"]["tenant"]["id"] = OTHER
    else:
        data["conversation"]["tenantId"] = OTHER
    with pytest.raises(AuthenticationError):
        await cached_validator(signing_key).validate(
            authorization(signing_key), Activity.model_validate(data), credentials()
        )


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
async def test_algorithm_confusion(configured, signing_key, algorithm):
    with pytest.raises(AuthenticationError):
        await cached_validator(signing_key).validate(
            authorization(signing_key, algorithm=algorithm),
            Activity.model_validate(payload()),
            credentials(),
        )


@pytest.mark.parametrize("header", [None, "", "Basic x", "Bearer invalid", "Bearer a b"])
async def test_invalid_authorization(configured, header, signing_key):
    with pytest.raises(AuthenticationError):
        await cached_validator(signing_key).validate(
            header, Activity.model_validate(payload()), credentials()
        )


async def test_missing_claim_and_endorsement(configured, signing_key):
    validator = cached_validator(signing_key)
    header = authorization(signing_key)
    claims = jwt.decode(header[7:], options={"verify_signature": False})
    del claims["exp"]
    with pytest.raises(AuthenticationError):
        await validator.validate(
            "Bearer "
            + jwt.encode(claims, signing_key[0], algorithm="RS256", headers={"kid": "known"}),
            Activity.model_validate(payload()),
            credentials(),
        )
    validator.keys = {"known": {**signing_key[1], "endorsements": ["webchat"]}}
    with pytest.raises(AuthenticationError):
        await validator.validate(header, Activity.model_validate(payload()), credentials())


async def test_keys_discovery_rotation_and_unknown_once(configured, signing_key, monkeypatch):
    calls = []
    available_keys = [signing_key[1]]
    real = httpx.AsyncClient

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == security.OPENID:
            return httpx.Response(
                200,
                json={
                    "issuer": security.ISSUER,
                    "jwks_uri": security.KEYS_URL,
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        return httpx.Response(200, json={"keys": available_keys})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    validator = Validator()
    activity = Activity.model_validate(payload())
    await validator.validate(authorization(signing_key), activity, credentials())
    await validator.validate(authorization(signing_key), activity, credentials())
    assert len(calls) == 2
    validator.refreshed -= 10
    with pytest.raises(AuthenticationError):
        await validator.validate(authorization(signing_key, kid="unknown"), activity, credentials())
    assert len(calls) == 4
    with pytest.raises(AuthenticationError):
        await validator.validate(authorization(signing_key, kid="unknown"), activity, credentials())
    assert len(calls) == 4
    available_keys.append({**signing_key[1], "kid": "rotated"})
    validator.refreshed -= 10
    validator.unknown_refreshed -= 10
    await validator.validate(authorization(signing_key, kid="rotated"), activity, credentials())
    assert len(calls) == 6


@pytest.mark.parametrize(
    "url,result",
    [
        (URL, THREAD),
        (URL.replace("%40", "%2540"), None),
        ("https://teams.microsoft.com/meet/123456789?p=pass", None),
        ("https://teams.microsoft.com.evil/l/meetup-join/" + THREAD, None),
        ("http://teams.microsoft.com/l/meetup-join/" + THREAD, None),
        (None, None),
        ("https://teams.microsoft.com/l/meetup-join/" + THREAD + "/0", THREAD),
    ],
)
def test_thread_extraction(url, result):
    assert store.thread_id(url) == result


def test_activity_schema_mentions_commands():
    assert Activity.model_validate(payload()).command_text() is None
    assert Activity.model_validate(payload(personal=True)).command_text() == "yardım"
    assert (
        Activity.model_validate(payload(text="Not al: Türkçe İÇERİK", mention=True)).command_text()
        == "Not al: Türkçe İÇERİK"
    )
    for text in ("sesli cevap ver", "ayrıl", "yönetici talimatı", "bilinmiyor"):
        assert service.command(text).intent == Intent.HELP
    for text, intent in (
        ("not al: Çağrı", Intent.NOTE),
        ("karar: Şişli", Intent.DECISION),
        ("aksiyon: İletişim", Intent.ACTION),
        ("özetle", Intent.SUMMARY),
    ):
        result = service.command(text)
        assert result.intent == intent and result.level == Level.PUBLIC
    invalid = payload()
    invalid["text"] = 3
    with pytest.raises(ValidationError):
        Activity.model_validate(invalid)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "https://evil.example/",
        "https://smba.trafficmanager.net@evil.example/",
        "https://smba.trafficmanager.net/?x=1",
    ],
)
def test_service_url_no_ssrf(url):
    data = payload()
    data["serviceUrl"] = url
    with pytest.raises(ValidationError):
        Activity.model_validate(data)


async def test_link_encryption_reference_update_and_deletion(database, encrypted):
    await repo.upsert_meeting("m1", platform="teams", url=URL)
    activity = Activity.model_validate(payload(kind="conversationUpdate"))
    assert (await store.linked_meeting(activity)).id == "m1"
    refs = await store.references("m1")
    assert len(refs) == 1 and refs[0].reference == activity.reference()
    async with session_scope() as session:
        raw = (await session.execute(select(TeamsConversation.__table__))).mappings().one()
        assert raw["reference"] == {} and raw["encrypted_content"]
        assert THREAD not in json.dumps(dict(raw))
    data = payload(kind="conversationUpdate")
    data["serviceUrl"] = "https://smba.trafficmanager.net/amer/"
    await store.linked_meeting(Activity.model_validate(data))
    assert (await store.references("m1"))[0].reference["service_url"] == data["serviceUrl"]
    assert await repo.delete_meeting("m1")
    assert await store.references("m1") == []


async def test_no_key_and_short_url_not_linked(database, configured):
    await repo.upsert_meeting("m1", platform="teams", url=URL)
    assert await store.save_reference("m1", Activity.model_validate(payload()).reference()) is None
    assert await store.references("m1") == []
    await repo.upsert_meeting(
        "m2", platform="teams", url="https://teams.microsoft.com/meet/123?p=x"
    )
    data = payload()
    data["conversation"]["id"] = "19:meeting_other@thread.v2"
    data["channelData"]["meeting"] = {"id": "123"}
    assert await store.linked_meeting(Activity.model_validate(data)) is None


async def test_bind_short_link_meeting_requires_manage_and_single_live(
    database, encrypted, fake_connector, monkeypatch
):
    """Kısa /meet bağlantısında sohbet, yöneten kişinin "@Katip bağla" komutuyla bağlanır."""
    _, sent = fake_connector
    short = "https://teams.microsoft.com/meet/123?p=x"
    owner = await repo.set_panel_account("owner@example.com", "unused", role="operator")
    await repo.upsert_meeting(
        "m1", platform="teams", url=short, owner_account_id=owner.id, title="Bütçe"
    )
    live = SimpleNamespace(session_id="m1", mode="tam", paused=False)
    monkeypatch.setattr(service.registry, "all", lambda: [live])
    monkeypatch.setattr(service.registry, "get", lambda mid: live if mid == "m1" else None)
    data = payload(text="bağla", mention=True)
    data["conversation"]["id"] = "19:meeting_short@thread.v2"
    await service.dispatch(Activity.model_validate(data))
    assert "bağlandı: Bütçe" in sent[-1][1]["text"]
    # Bağlandıktan sonra aynı sohbet toplantıya çözülür.
    assert (await store.linked_meeting(Activity.model_validate(data))).id == "m1"
    await service.dispatch(Activity.model_validate(data))
    assert "zaten bağlı" in sent[-1][1]["text"]

    # Yetkisiz kişi bağlayamaz; başka bir sohbet bağlı kalmaz.
    other = payload(text="bağla", mention=True)
    other["conversation"]["id"] = "19:meeting_foreign@thread.v2"
    stranger = await repo.set_panel_account("stranger@example.com", "unused", role="operator")
    assert stranger
    fake, _ = fake_connector

    async def stranger_member(activity):
        return {"email": "stranger@example.com"}

    monkeypatch.setattr(fake, "member", stranger_member)
    await service.dispatch(Activity.model_validate(other))
    assert "canlı bir Teams toplantısı yok" in sent[-1][1]["text"]
    assert await store.linked_meeting(Activity.model_validate(other)) is None

    # Birden çok aday varsa tahmin edilmez.
    monkeypatch.setattr(fake, "member", lambda activity: _owner())
    await repo.upsert_meeting("m2", platform="teams", url=short + "2", owner_account_id=owner.id)
    live2 = SimpleNamespace(session_id="m2", mode="tam", paused=False)
    monkeypatch.setattr(service.registry, "all", lambda: [live, live2])
    third = payload(text="bağla", mention=True)
    third["conversation"]["id"] = "19:meeting_third@thread.v2"
    await service.dispatch(Activity.model_validate(third))
    assert "birden çok canlı toplantı" in sent[-1][1]["text"]


async def _owner():
    return {"email": "owner@example.com"}


async def test_forget_removes_references(database, encrypted):
    await repo.upsert_meeting("m1", platform="teams", url=URL)
    await store.save_reference("m1", Activity.model_validate(payload()).reference())
    person = await repo.ensure_person_by_email("participant@example.com", "Katılımcı")
    assert await repo.forget_person(person.id, requested_by="admin")
    assert await store.references("m1") == []


@pytest.fixture
async def fake_connector(monkeypatch):
    sent = []

    async def member(activity):
        return {"email": "owner@example.com"}

    async def send(reference, **kwargs):
        sent.append((reference, kwargs))
        return {"id": "sent"}

    fake = SimpleNamespace(member=member, send=send)
    connector.set(fake)
    return fake, sent


async def test_help_is_markdown_and_personal_help_introduces_katip(
    database, encrypted, fake_connector
):
    _, sent = fake_connector
    await service.dispatch(Activity.model_validate(payload(text="yardım", personal=True)))
    await service.dispatch(Activity.model_validate(payload(text="yardım", mention=True)))
    assert sent[0][1] == {"text": service.HELP_PERSONAL, "markdown": True}
    assert sent[1][1] == {"text": service.HELP, "markdown": True}
    # Toplantıdan türeyen cevaplar markdown olarak yorumlanmaz.
    await service.dispatch(Activity.model_validate(payload(text="durum", mention=True)))
    assert sent[2][1]["markdown"] is False


async def test_routes_live_commands_and_shared_budget(database, encrypted, fake_connector):
    owner = await repo.set_panel_account("owner@example.com", "unused", role="operator")
    await repo.upsert_meeting("m1", platform="teams", url=URL, owner_account_id=owner.id)
    summary_calls = []

    async def summarize(**kwargs):
        summary_calls.append(kwargs)
        return "Güncel özet"

    live = SimpleNamespace(
        session_id="m1",
        mode="kayit",
        paused=False,
        identifier=None,
        transcript_text=lambda: "Toplantı metni",
    )
    live.agent = MeetingAgent(live, llm_module=SimpleNamespace(summarize_so_far=summarize))
    from katip.pipeline.session import registry

    registry._sessions["m1"] = live
    _, sent = fake_connector
    await service.dispatch(Activity.model_validate(payload(text="not al: Çağrı", mention=False)))
    assert sent == []
    for text in (
        "not al: Çağrı",
        "karar: Şişli",
        "aksiyon: İletişim",
        "durum",
        "özetle",
        "rapor",
        "ayrıl",
    ):
        await service.dispatch(Activity.model_validate(payload(text=text, mention=True)))
    notes = await repo.list_notes("m1")
    assert [(n.kind, n.text) for n in notes] == [
        ("note", "Çağrı"),
        ("decision", "Şişli"),
        ("action", "İletişim"),
    ]
    assert len(summary_calls) == 1 and live.agent.llm_calls == 1
    assert "sessiz kayıt" in sent[3][1]["text"]
    assert sent[-1][1]["text"] == service.HELP
    encrypted.max_agent_calls_per_meeting = 1
    await service.dispatch(Activity.model_validate(payload(text="özetle", personal=True)))
    assert "limit" in sent[-1][1]["text"] and len(summary_calls) == 1
    live.paused = True
    await service.dispatch(Activity.model_validate(payload(text="not al: Gizli", mention=True)))
    assert len(await repo.list_notes("m1")) == 3


async def test_outbound_http_token_cache_expiry_and_tenant(configured, monkeypatch):
    calls = []
    real = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        if request.url.host == "login.microsoftonline.com":
            assert TENANT in request.url.path
            assert b"scope=https%3A%2F%2Fapi.botframework.com%2F.default" in request.content
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer token"
        if "/members/" in request.url.path:
            return httpx.Response(200, json={"aadObjectId": AAD, "email": "OWNER@EXAMPLE.COM"})
        return httpx.Response(200, json={"id": "posted"})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    client = Connector()
    activity = Activity.model_validate(payload())
    assert await client.member(activity) == {"email": "owner@example.com"}
    await asyncio.gather(
        client.send(activity.reference(), text="Bir"), client.send(activity.reference(), text="İki")
    )
    assert sum(r.url.host == "login.microsoftonline.com" for r in calls) == 1
    client.expires = 0
    await client.send(activity.reference(), text="Üç")
    assert sum(r.url.host == "login.microsoftonline.com" for r in calls) == 2
    with pytest.raises(RuntimeError):
        await client.send({**activity.reference(), "tenant": OTHER}, text="Yasak")


async def seed_mail():
    owner = await repo.set_panel_account("owner@example.com", "unused", role="operator")
    await repo.upsert_meeting(
        "m1", platform="teams", url=URL, title="Bütçe", owner_account_id=owner.id
    )
    await repo.set_report("m1", REPORT, {})
    return owner


def invoke_activity(draft):
    data = payload(personal=True, kind="invoke")
    data.update(
        name="adaptiveCard/action",
        value={
            "action": {
                "type": "Action.Execute",
                "verb": "mail.send",
                "data": {
                    "meeting_id": "m1",
                    "draft_id": draft["draft_id"],
                    "version": draft["version"],
                },
            }
        },
    )
    return Activity.model_validate(data)


async def test_approval_concurrent_send_once_nonowner_version_and_flag(
    database, encrypted, fake_connector, monkeypatch
):
    owner = await seed_mail()
    encrypted.mail_enabled = True
    encrypted.m365_client_id, encrypted.m365_client_secret = "mail-client", "mail-secret"
    encrypted.m365_tenant_id = TENANT
    draft = await mail.get_draft("m1", PanelAccess(owner.id, owner.role))
    from katip.integrations.microsoft import MicrosoftGraph

    calls = []

    async def send_mail(self, raw, request_id):
        calls.append(request_id)
        await asyncio.sleep(0)
        return httpx.Response(202)

    monkeypatch.setattr(MicrosoftGraph, "send_mail", send_mail)
    fake, _ = fake_connector

    async def stranger(activity):
        return {"email": "stranger@example.com"}

    await repo.set_panel_account("stranger@example.com", "unused", role="operator")
    fake.member = stranger
    with pytest.raises(HTTPException):
        await cards.invoke(invoke_activity(draft))
    assert calls == []

    async def owner_member(activity):
        return {"email": owner.email}

    fake.member = owner_member
    stale = invoke_activity({**draft, "version": draft["version"] + 1})
    response = await cards.invoke(stale)
    assert "Taslak değişti" in json.dumps(response, ensure_ascii=False) and not calls
    encrypted.mail_enabled = False
    response = await cards.invoke(invoke_activity(draft))
    assert not calls and response["statusCode"] == 200
    encrypted.mail_enabled = True
    responses = await asyncio.gather(*[cards.invoke(invoke_activity(draft)) for _ in range(5)])
    assert len(calls) == 1
    assert all(r["statusCode"] == 200 for r in responses)
    final = await cards.invoke(invoke_activity(draft))
    assert "Gönderildi" in json.dumps(final, ensure_ascii=False)
    assert len(calls) == 1


async def test_proactive_report_card_once_notify_off(database, encrypted, fake_connector):
    owner = await seed_mail()
    reference = Activity.model_validate(payload()).reference()
    await store.save_reference("m1", reference)
    await store.save_reference("m1", {**reference, "conversation_id": "personal"}, owner.id)
    encrypted.mail_enabled = True
    encrypted.m365_tenant_id, encrypted.m365_client_id, encrypted.m365_client_secret = (
        TENANT,
        "client",
        "secret",
    )
    _, sent = fake_connector
    encrypted.teams_notify = False
    await service.report_ready("m1")
    assert sent == []
    encrypted.teams_notify = True
    await asyncio.gather(service.report_ready("m1"), service.report_ready("m1"))
    assert len(sent) == 2
    assert "Gizli özet" not in json.dumps(sent, ensure_ascii=False)
    group_message = next(body["text"] for ref, body in sent if "text" in body)
    assert "Bütçe" in group_message and "https://katip.example/panel/m1?t=" in group_message
    card = next(body["card"] for ref, body in sent if "card" in body)
    assert card["actions"][0]["type"] == "Action.Execute"
    assert "owner@example.com" not in json.dumps(card)
    await service.report_ready("m1")
    assert len(sent) == 2


async def test_owner_not_installed_keeps_panel(database, encrypted, fake_connector):
    await seed_mail()
    await store.save_reference("m1", Activity.model_validate(payload()).reference())
    encrypted.mail_enabled = True
    encrypted.m365_tenant_id, encrypted.m365_client_id, encrypted.m365_client_secret = (
        TENANT,
        "client",
        "secret",
    )
    fake, sent = fake_connector

    async def request(reference, method, path, body=None):
        if method == "GET":
            return [{"email": "owner@example.com", "aadObjectId": AAD}]
        assert body["members"] == [{"id": AAD}]
        raise httpx.HTTPStatusError(
            "not installed",
            request=httpx.Request("POST", SERVICE_URL),
            response=httpx.Response(403),
        )

    fake.request = request
    await service.report_ready("m1")
    assert len(sent) == 1 and "card" not in sent[0][1]


def test_card_external_highlight_and_data_minimization(configured):
    draft = {
        "status": "taslak",
        "subject": "Onay",
        "draft_id": "a" * 32,
        "version": 2,
        "recipients": ["one@example.com", "two@external.example"],
        "body_text": "Gizli gövde",
    }
    card = cards.approval_card("m1", draft)
    assert card["body"][3]["color"] == "Attention"
    assert "Dış alıcı sayısı: 1" == card["body"][3]["text"]
    assert card["body"][4]["color"] == "Attention"
    assert "two@external.example" in card["body"][4]["text"]
    assert "one@example.com" not in json.dumps(card) and "Gizli gövde" not in json.dumps(card)
    assert set(card["actions"][0]["data"]) == {"meeting_id", "draft_id", "version"}
    assert "@" not in json.dumps(card["actions"][0]["data"])


def test_package_manifest_icons_no_secrets(configured):
    first = manifest()
    assert first["manifestVersion"] == "1.30"
    assert first["name"]["short"] == "Katip"
    assert first["bots"][0]["botId"] == APP_ID
    assert first["bots"][0]["scopes"] == ["personal", "groupChat"]
    # Teams yüklemesi: 1.25+ manifestte "team" kapsamı supportsChannelFeatures ister.
    assert all("team" not in c["scopes"] for c in first["bots"][0]["commandLists"])
    assert "supportsChannelFeatures" not in first
    assert first["bots"][0]["isNotificationOnly"] is False
    assert first["validDomains"] == ["katip.example"]
    assert "staticTabs" not in first
    assert first["version"] == "1.0.1"
    assert first["webApplicationInfo"] == {"id": APP_ID, "resource": "api://botid-" + APP_ID}
    assert first["authorization"]["permissions"]["resourceSpecific"] == [
        {"name": "OnlineMeeting.ReadBasic.Chat", "type": "Application"}
    ]
    assert {"katıl", "bağla"} <= {
        c["title"] for c in first["bots"][0]["commandLists"][0]["commands"]
    }
    assert manifest()["id"] == first["id"]
    with zipfile.ZipFile(io.BytesIO(build_package())) as archive:
        assert set(archive.namelist()) == {"manifest.json", "color.png", "outline.png"}
        for name, size in (("color.png", 192), ("outline.png", 32)):
            assert struct.unpack(">II", archive.read(name)[16:24]) == (size, size)
        assert configured.teams_bot_secret not in archive.read("manifest.json").decode()
    configured.teams_app_package_id = OTHER
    assert manifest()["id"] == OTHER
    configured.public_base_url = "http://katip.example"
    with pytest.raises(ValueError):
        build_package()


async def test_endpoint_off_auth_schema_body_and_rate(database, configured, signing_key):
    security.validator.set(cached_validator(signing_key))
    security.limits.set(security.RateLimit())
    real = httpx.AsyncClient
    async with real(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        configured.teams_bot_secret = ""
        assert (await client.post("/api/messages", json=payload())).status_code == 404
        configured.teams_bot_secret = "secret"
        assert (await client.post("/api/messages", json=payload())).status_code == 401
        headers = {"Authorization": authorization(signing_key)}
        assert (
            await client.post(
                "/api/messages",
                content=b"x" * (128 * 1024 + 1),
                headers={**headers, "Content-Type": "application/json"},
            )
        ).status_code == 413
        assert (
            await client.post(
                "/api/messages",
                content='{"text":1,"text":2}',
                headers={**headers, "Content-Type": "application/json"},
            )
        ).status_code == 400
        assert (
            await client.post("/api/messages", json=payload(), headers=headers)
        ).status_code == 200
        for _ in range(29):
            assert (
                await client.post("/api/messages", json=payload(), headers=headers)
            ).status_code == 200
        assert (
            await client.post("/api/messages", json=payload(), headers=headers)
        ).status_code == 429


async def test_admin_package_permission(database, configured, monkeypatch):
    from katip.api.auth import hash_password

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.get("/api/admin/teams/package")).status_code == 401
        response = await client.get(
            "/api/admin/teams/package", headers={"Authorization": "Bearer test-admin"}
        )
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "no-store"
        assert zipfile.is_zipfile(io.BytesIO(response.content))
        password = "Teams-Account-Test-2026!"
        for role in ("viewer", "operator"):
            await repo.set_panel_account(f"{role}@example.com", hash_password(password), role=role)
            login = await client.post(
                "/api/auth/login",
                json={
                    "email": f"{role}@example.com",
                    "password": password,
                },
            )
            assert login.status_code == 200
            assert (await client.get("/api/admin/teams/package")).status_code == 403
            client.cookies.clear()


def test_outline_white_and_transparent():
    from katip.teams.package import assets_dir

    raw = (assets_dir() / "outline.png").read_bytes()
    offset, compressed = 8, bytearray()
    while offset < len(raw):
        size = struct.unpack(">I", raw[offset : offset + 4])[0]
        if raw[offset + 4 : offset + 8] == b"IDAT":
            compressed.extend(raw[offset + 8 : offset + 8 + size])
        offset += size + 12
    pixels = zlib.decompress(compressed)
    alphas = []
    for y in range(32):
        row = pixels[y * 129 : (y + 1) * 129]
        assert row[0] == 0
        for x in range(32):
            assert row[1 + x * 4 : 4 + x * 4] == b"\xff\xff\xff"
            alphas.append(row[4 + x * 4])
    assert min(alphas) == 0 and max(alphas) == 255


async def test_invoke_endpoint_jwt_and_feature_off(database, configured, signing_key, monkeypatch):
    security.validator.set(cached_validator(signing_key))
    data = invoke_activity({"draft_id": "a" * 32, "version": 1}).model_dump(by_alias=True)
    calls = []

    async def dispatch(activity):
        calls.append(activity.type)
        raise HTTPException(403, "yetkisiz")

    monkeypatch.setattr("katip.api.routes.teams.dispatch", dispatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (await client.post("/api/messages", json=data)).status_code == 401
        assert calls == []
        configured.teams_bot_secret = ""
        assert (await client.post("/api/messages", json=data)).status_code == 404
        assert calls == []
        configured.teams_bot_secret = "secret"
        response = await client.post(
            "/api/messages",
            json=data,
            headers={
                "Authorization": authorization(signing_key),
            },
        )
        assert response.status_code == 200 and response.json()["statusCode"] == 403
        assert calls == ["invoke"]


async def test_conversation_update_routes_and_stores(database, encrypted, fake_connector):
    await repo.upsert_meeting("m1", platform="teams", url=URL)
    await service.dispatch(Activity.model_validate(payload(kind="conversationUpdate")))
    assert len(await store.references("m1")) == 1
    assert fake_connector[1] == []


def test_tenant_rate_limit():
    limiter = security.RateLimit()
    assert all(limiter.take("tenant", 300) for _ in range(300))
    assert not limiter.take("tenant", 300)


def test_secret_file_tenant_fallback_health(configured, tmp_path):
    configured.teams_bot_tenant_id = ""
    configured.m365_tenant_id = TENANT
    file = tmp_path / "bot-secret"
    file.write_text("file-secret", encoding="utf-8")
    configured.teams_bot_secret_file = str(file)
    assert credentials().secret == "file-secret" and credentials().tenant == TENANT
    assert "file-secret" not in repr(credentials())
    security.last_validation_error = False
    assert security.health() == (True, "hazır")
    security.last_validation_error = True
    assert security.health() == (False, "son doğrulama hatası")
    file.unlink()
    assert credentials() is None and security.health() == (None, "kapalı")


def meeting_activity(*, text="durum", kind="message", mention=True):
    data = payload(text=text, kind=kind, mention=mention)
    data["channelData"]["meeting"] = {"id": "meeting/opaque+id="}
    return Activity.model_validate(data)


@pytest.fixture
def meeting_connector(fake_connector):
    fake, sent = fake_connector
    fake.meeting_details = AsyncMock(
        return_value={"details": {"joinUrl": URL, "title": "Çağrı"}, "conversation": {"id": THREAD}}
    )
    return fake, sent


@pytest.fixture
async def routing_session(database, encrypted):
    from katip.pipeline.session import MeetingSession, registry

    await repo.upsert_meeting("m1", platform="teams", url=URL)
    live = MeetingSession("m1", stt=SimpleNamespace(translates=True), persist=False)
    registry._sessions["m1"] = live
    live.agent.on_chat = AsyncMock()
    yield live
    await live.chat_router.flush()
    if live._final_worker:
        live._final_queue.put_nowait(None)
        await live._final_worker
    for task in list(live._background):
        task.cancel()
    await asyncio.gather(*live._background, return_exceptions=True)


async def drain_background(live):
    while live._background:
        await asyncio.gather(*live._background)


async def test_bound_session_routes_all_chat_and_ignores_browser_input(
    routing_session, fake_connector, monkeypatch
):
    from katip.pipeline.stt.base import Utterance
    from katip.privacy import late_joiner_notice, privacy_notice_chat

    live = routing_session
    await store.bind("m1", meeting_activity())
    messages = [
        privacy_notice_chat(),
        late_joiner_notice(),
        "Yazılı cevap",
        "Not alındı.",
        "Karar kaydedildi.",
        "Aksiyon eklendi.",
        "Rapor hazır: https://katip.example/panel/m1",
        "Kullanılabilir komutlar",
        "İşlem tamamlanamadı.",
    ]
    for message in messages:
        live.say(message, channel="chat")
    # Bağlanmadan önce kuyrukta kalmış bir DOM mesajı da işlenmez.
    live._queue_browser_chat("Ayşe", "Katip durum")
    live.on_chat("Ayşe", "Katip not al: Tek kayıt")
    live._process_final = AsyncMock()
    voice = Utterance("v1", "Katip, durum", True, 0, 1, "1", "tr")
    live._on_utterance(voice)
    await drain_background(live)
    live._final_queue.put_nowait(None)
    await live._final_worker
    live._final_worker = None
    assert not live.outbox.qsize()
    assert [body for _, body in fake_connector[1]] == [{"text": m} for m in messages]
    live.agent.on_chat.assert_not_awaited()
    live._process_final.assert_awaited_once()
    spoken = AsyncMock()
    monkeypatch.setattr(live, "_speak", spoken)
    live.say("Sesli cevap", channel="voice")
    await drain_background(live)
    spoken.assert_awaited_once()
    assert len(fake_connector[1]) == len(messages)


@pytest.mark.parametrize("platform", ["teams", "zoom", "meet"])
async def test_unbound_session_keeps_browser_chat(routing_session, fake_connector, platform):
    live = routing_session
    await repo.upsert_meeting("m1", platform=platform)
    live.say("Merhaba", channel="chat")
    live.on_chat("Ayşe", "Katip durum")
    await drain_background(live)
    live._final_queue.put_nowait(None)
    await live._final_worker
    live._final_worker = None
    assert live.outbox.get_nowait() == {"kind": "chat_send", "text": "Merhaba"}
    live.agent.on_chat.assert_awaited_once_with("Ayşe", "Katip durum")
    assert fake_connector[1] == []


async def test_personal_reference_does_not_own_chat(routing_session, fake_connector):
    owner = await repo.set_panel_account("owner@example.com", "unused", role="operator")
    await store.save_reference("m1", meeting_activity().reference(), owner.id)
    routing_session.say("Merhaba", channel="chat")
    await drain_background(routing_session)
    assert routing_session.outbox.get_nowait()["text"] == "Merhaba"
    assert fake_connector[1] == []


@pytest.mark.parametrize("binding", ["before", "after"])
@pytest.mark.parametrize("mode", ["tam", "kayit"])
async def test_one_privacy_notice_before_or_after_binding(
    routing_session, fake_connector, monkeypatch, encrypted, binding, mode
):
    from katip.privacy import privacy_notice_chat

    live = routing_session
    live.mode = mode
    encrypted.privacy_notice_on_join = "voice" if mode == "kayit" else "chat"
    original_sleep = asyncio.sleep

    async def no_delay(delay):
        await original_sleep(0)

    monkeypatch.setattr("katip.pipeline.session.asyncio.sleep", no_delay)
    if binding == "before":
        await store.bind("m1", meeting_activity())
    await asyncio.gather(live._announce(), live._announce())
    await drain_background(live)
    if binding == "after":
        await store.bind("m1", meeting_activity())
        await live._announce()
        await drain_background(live)
    browser = [live.outbox.get_nowait() for _ in range(live.outbox.qsize())]
    app_texts = [body["text"] for _, body in fake_connector[1]]
    assert (
        app_texts.count(privacy_notice_chat())
        + sum(item.get("text") == privacy_notice_chat() for item in browser)
        == 1
    )
    assert len(app_texts) == (1 if binding == "before" else 0)
    assert len(browser) == (0 if binding == "before" else 1)


async def test_teams_send_failure_preserves_privacy_via_browser(
    routing_session, fake_connector, monkeypatch, encrypted, caplog
):
    from katip.privacy import privacy_notice_chat

    await store.bind("m1", meeting_activity())
    fake_connector[0].send = AsyncMock(side_effect=RuntimeError("GİZLİ İÇERİK"))
    routing_session.say(privacy_notice_chat(), channel="chat")
    await drain_background(routing_session)
    assert routing_session.outbox.get_nowait() == {
        "kind": "chat_send",
        "text": privacy_notice_chat(),
    }
    assert "GİZLİ İÇERİK" not in caplog.text
    routing_session.on_chat("Ayşe", "Katip durum")
    await drain_background(routing_session)
    routing_session.agent.on_chat.assert_not_awaited()
    encrypted.teams_bot_secret = ""
    routing_session.say("Tarayıcı", channel="chat")
    assert routing_session.outbox.get_nowait()["text"] == "Tarayıcı"


@pytest.mark.parametrize("form", ["short", "long", "thread"])
@pytest.mark.parametrize("kind", ["installationUpdate", "conversationUpdate", "message"])
async def test_automatic_binding_from_meeting_details(
    database, encrypted, meeting_connector, form, kind
):
    fake, sent = meeting_connector
    join_url = "https://teams.microsoft.com/meet/123456789?p=pass" if form == "short" else URL
    panel_url = join_url.split("?")[0] + "?tracking=different"
    if form == "thread":
        join_url = "https://teams.microsoft.com/meet/123456789?p=pass"
    fake.meeting_details.return_value["details"]["joinUrl"] = join_url
    await repo.upsert_meeting("m1", platform="teams", url=panel_url)
    activity = meeting_activity(kind=kind, mention=False)
    await service.dispatch(activity)
    await service.dispatch(activity)
    assert len(await store.references("m1")) == 1
    assert [body["text"] for _, body in sent] == ["Katip bu toplantı sohbetine bağlandı."]
    fake.meeting_details.assert_awaited_once_with(activity)


@pytest.mark.parametrize("count", [0, 2])
async def test_automatic_binding_requires_exactly_one_match(
    database, encrypted, meeting_connector, count
):
    for number in range(count):
        await repo.upsert_meeting(f"m{number}", platform="teams", url=URL)
    await service.dispatch(meeting_activity(kind="conversationUpdate"))
    for number in range(count):
        assert await store.references(f"m{number}") == []
    assert meeting_connector[1] == []


async def test_auto_binding_excludes_old_and_non_teams(database, encrypted, meeting_connector):
    from datetime import UTC, datetime, timedelta

    from katip.store.db import write_scope
    from katip.store.models import Meeting

    await repo.upsert_meeting("old", platform="teams", url=URL)
    await repo.upsert_meeting("zoom", platform="zoom", url=URL)
    async with write_scope() as session:
        old = await session.get(Meeting, "old")
        old.started_at = datetime.now(UTC) - timedelta(days=3)
    await service.dispatch(meeting_activity(kind="conversationUpdate"))
    assert await store.references("old") == []
    assert meeting_connector[1] == []


@pytest.fixture
def join_launcher(monkeypatch):
    from katip.pipeline.session import MeetingSession, registry

    launched = []

    def create(mid, **kwargs):
        live = MeetingSession(mid, stt=SimpleNamespace(translates=True), persist=False, **kwargs)
        registry._sessions[mid] = live
        return live

    async def start(mid, url, **kwargs):
        assert len(await store.references(mid)) == 1
        launched.append((mid, url, kwargs))

    monkeypatch.setattr(registry, "create", create)
    monkeypatch.setattr(service.meeting_routes.supervisor, "start", start)
    monkeypatch.setattr("katip.integrations.microsoft.sync_identities", AsyncMock())
    return launched


@pytest.mark.parametrize(
    "role,enabled,allowed",
    [
        (None, True, False),
        ("viewer", True, False),
        ("operator", False, False),
        ("admin", False, False),
        ("operator", True, True),
        ("admin", True, True),
    ],
)
async def test_join_permission_matrix_uses_real_flow(
    database, encrypted, meeting_connector, join_launcher, role, enabled, allowed
):
    if role:
        account = await repo.set_panel_account("owner@example.com", "unused", role=role)
        if not enabled:
            await repo.disable_panel_account(account.id)
    encrypted.default_mode = "kayit"
    await service.dispatch(meeting_activity(text="katıl"))
    sent = meeting_connector[1]
    if allowed:
        assert sent[-1][1]["text"] == "Katip toplantıya katılıyor; lobiden kabul edin."
        assert len(join_launcher) == 1
        mid = join_launcher[0][0]
        row = await repo.get_meeting(mid)
        assert row.owner_account_id == account.id
        assert service.registry.get(mid).mode == "kayit"
        audit = (await repo.recent_audit(("meeting.joined",)))[0]
        assert audit.actor == f"panel:{account.id}" and audit.detail["meeting_id"] == mid
        await drain_background(service.registry.get(mid))
    else:
        assert "açık bir operatör veya yönetici" in sent[-1][1]["text"]
        assert join_launcher == []


@pytest.mark.parametrize("limit", ["bots", "minutes", "workers"])
async def test_join_shared_limits(
    database, encrypted, meeting_connector, join_launcher, monkeypatch, limit
):
    from katip.bot.supervisor import LimitExceeded

    await repo.set_panel_account("owner@example.com", "unused", role="operator")
    if limit == "bots":

        def full():
            raise LimitExceeded("Bot kapasitesi dolu")

        monkeypatch.setattr(service.meeting_routes.supervisor, "check_capacity", full)
    elif limit == "minutes":
        monkeypatch.setattr(repo, "meeting_minutes_today", AsyncMock(return_value=10**6))
    else:
        monkeypatch.setattr(
            service.meeting_routes.worker_jobs, "count_open", AsyncMock(return_value=100)
        )
    await service.dispatch(meeting_activity(text="katıl"))
    assert "Katip katılamadı:" in meeting_connector[1][-1][1]["text"]
    assert join_launcher == []


async def test_concurrent_join_starts_one_bot(
    database, encrypted, meeting_connector, join_launcher
):
    await repo.set_panel_account("owner@example.com", "unused", role="operator")
    await asyncio.gather(*[service.dispatch(meeting_activity(text="katıl")) for _ in range(3)])
    assert len(join_launcher) == 1
    texts = [body["text"] for _, body in meeting_connector[1]]
    assert texts.count("Katip toplantıya katılıyor; lobiden kabul edin.") == 1
    assert sum("zaten canlı" in text for text in texts) == 2
    await drain_background(service.registry.get(join_launcher[0][0]))


@pytest.mark.parametrize("failure", ["rsc", "missing", "unsafe"])
async def test_meeting_details_failure_helpful_no_join(
    database, encrypted, meeting_connector, join_launcher, failure
):
    await repo.set_panel_account("owner@example.com", "unused", role="operator")
    fake, sent = meeting_connector
    if failure == "rsc":
        fake.meeting_details.side_effect = httpx.HTTPStatusError(
            "secret body", request=httpx.Request("GET", SERVICE_URL), response=httpx.Response(403)
        )
    elif failure == "missing":
        fake.meeting_details.return_value = {"details": {}}
    else:
        fake.meeting_details.return_value = {"details": {"joinUrl": "https://evil.example/"}}
    await service.dispatch(meeting_activity(text="katıl"))
    assert "kaldırıp yeniden ekleyin" in sent[-1][1]["text"]
    assert "OnlineMeeting.ReadBasic.Chat" in sent[-1][1]["text"]
    assert fake.meeting_details.await_count == 1
    assert join_launcher == []


async def test_meeting_details_http_v1_path_and_bot_token(configured, monkeypatch):
    calls = []
    real = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "bot-token", "expires_in": 3600})
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer bot-token"
        assert request.url.raw_path == b"/emea/v1/meetings/meeting%2Fopaque%2Bid%3D"
        return httpx.Response(200, json={"details": {"joinUrl": URL}})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    result = await Connector().meeting_details(meeting_activity())
    assert result["details"]["joinUrl"] == URL and len(calls) == 2


async def test_proactive_sends_are_rate_limited(configured, monkeypatch):
    now = [100.0]
    waits = []
    client = Connector()
    monkeypatch.setattr("katip.teams.client.time.monotonic", lambda: now[0])

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    monkeypatch.setattr("katip.teams.client.asyncio.sleep", sleep)
    client.request = AsyncMock(return_value={"id": "sent"})
    reference = meeting_activity().reference()
    await asyncio.gather(*[client.send(reference, text="Düz **metin**") for _ in range(3)])
    await client.send({**reference, "conversation_id": "other"}, text="Başka sohbet")
    assert waits == pytest.approx([2, 2, 0.2])
    assert client.request.await_count == 4
    assert all(call.args[3]["textFormat"] == "plain" for call in client.request.await_args_list)
