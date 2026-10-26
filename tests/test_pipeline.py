"""Uçtan uca: bot soketinden PCM gönder, panelde segment + çeviri + konuşmacı adını gör.

STT ve çeviri sahte (stub) sağlayıcılarla değiştirilir — test ağa çıkmaz.
"""

from __future__ import annotations

import json
import struct
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from katip.api import security
from katip.api.main import app
from katip.pipeline import session as session_mod
from katip.pipeline.stt.base import STTProvider, Utterance

HEADER = struct.Struct("<II")


class StubSoniox(STTProvider):
    """Soniox gibi davranır: 3 frame sonra kısmi, 4. frame'de final + akış içi çeviri."""

    translates = True

    async def stream(
        self, audio: AsyncIterator[bytes], *, language: str, target_language: str | None = None
    ) -> AsyncIterator[Utterance]:
        count = 0
        async for _ in audio:
            count += 1
            if count == 3:
                yield Utterance(
                    "u1", "merhaba", False, 0.0, 0.3, "1", language, "hel", target_language
                )
            if count == 4:
                yield Utterance(
                    "u1",
                    "merhaba dunya",
                    True,
                    0.0,
                    0.6,
                    "1",
                    language,
                    "hello world",
                    target_language,
                )


class StubPlainSTT(STTProvider):
    """Çeviri vermeyen sağlayıcı (Deepgram gibi) → pipeline Claude'a çevirtir."""

    translates = False

    async def stream(
        self, audio: AsyncIterator[bytes], *, language: str, target_language: str | None = None
    ) -> AsyncIterator[Utterance]:
        count = 0
        async for _ in audio:
            count += 1
            if count == 3:
                yield Utterance("p1", "merhaba dunya", True, 0.0, 0.6, "1", language)


class StubTranslator:
    def __init__(self, source_lang=None, target_lang=None) -> None:
        self.target_lang = target_lang

    async def translate(self, text: str) -> str:
        return f"[en] {text}"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(session_mod, "Translator", StubTranslator)
    session_mod.registry._sessions.clear()
    yield
    session_mod.registry._sessions.clear()


ADMIN = {"Authorization": "Bearer test-admin"}


def _bot(client, sid: str):
    """Supervisor'ın yaptığı gibi toplantıya özel bot anahtarıyla bağlan."""
    token = security.issue_bot_token(sid)
    return client.websocket_connect(f"/ws/ingest/{sid}", headers={"X-Katip-Bot-Token": token})


def _panel(client, sid: str):
    return client.websocket_connect(f"/ws/panel/{sid}?{security.view_link_params(sid)}")


def _use(monkeypatch: pytest.MonkeyPatch, provider_cls: type[STTProvider]) -> None:
    monkeypatch.setattr(session_mod, "build_provider", lambda: provider_cls())


def _frame(seq: int, samples: int = 320) -> bytes:
    return HEADER.pack(0, seq) + b"\x00\x01" * samples


def _is_done(msg: dict) -> bool:
    """Segment ve çeviri olayları büyüyerek gelir; yalnızca final hâlleri 'tamam' sayılır."""
    body = msg.get(msg["kind"])
    return not isinstance(body, dict) or "is_final" not in body or body["is_final"]


def _collect(panel, want: set[str], limit: int = 20) -> dict[str, list[dict]]:
    seen: dict[str, list[dict]] = {}
    for _ in range(limit):
        msg = panel.receive_json()
        seen.setdefault(msg["kind"], []).append(msg)
        if want <= {k for k, msgs in seen.items() if any(_is_done(m) for m in msgs)}:
            break
    return seen


def test_soniox_like_provider_streams_segment_translation_and_speaker(
    monkeypatch, api_client
) -> None:
    _use(monkeypatch, StubSoniox)
    client = api_client
    sid = "s-soniox"
    session_mod.registry.create(sid)

    with _panel(client, sid) as panel:
        with _bot(client, sid) as bot:
            bot.send_text(
                json.dumps(
                    {"kind": "roster", "participants": ["Ali", "Ayse"], "active_speakers": ["Ali"]}
                )
            )
            for seq in range(5):
                bot.send_bytes(_frame(seq))
            seen = _collect(panel, {"segment", "translation", "roster"})

    segments = [m["segment"] for m in seen["segment"]]
    assert segments[0]["id"] == segments[-1]["id"] == "u1"  # aynı parça büyüyerek geldi
    assert segments[0]["is_final"] is False
    final = segments[-1]
    assert final["is_final"] is True
    assert final["text"] == "merhaba dunya"
    # One sentence/one active tile cannot establish a person's identity.
    assert final["speaker"] == "Konuşmacı 1"

    translations = [m["translation"] for m in seen["translation"]]
    assert translations[-1] == {
        "segment_id": "u1",
        "session_id": sid,
        "text": "hello world",
        "lang": "en",
        "is_final": True,
    }


def test_plain_provider_falls_back_to_claude_translation(monkeypatch, api_client) -> None:
    _use(monkeypatch, StubPlainSTT)
    client = api_client
    sid = "s-plain"
    session_mod.registry.create(sid)

    with _panel(client, sid) as panel:
        with _bot(client, sid) as bot:
            for seq in range(4):
                bot.send_bytes(_frame(seq))
            seen = _collect(panel, {"segment", "translation"})

    assert seen["translation"][-1]["translation"]["text"] == "[en] merhaba dunya"
    assert seen["translation"][-1]["translation"]["segment_id"] == "p1"


def test_transcript_endpoint_returns_final_segments_with_translation(
    monkeypatch, api_client
) -> None:
    _use(monkeypatch, StubSoniox)
    client = api_client
    sid = "s-transcript"
    session_mod.registry.create(sid)

    with _panel(client, sid) as panel:
        with _bot(client, sid) as bot:
            for seq in range(5):
                bot.send_bytes(_frame(seq))
            _collect(panel, {"segment", "translation"})

    body = client.get(f"/api/meetings/{sid}/transcript", headers=ADMIN).json()
    assert [(s["text"], s["translation"]) for s in body] == [("merhaba dunya", "hello world")]


ADMIN = {"Authorization": "Bearer test-admin"}


def test_join_requires_admin_and_rejects_unknown_platform() -> None:
    client = TestClient(app)
    with client:
        url = {"url": "https://meet.google.com/abc-defg-hij"}
        assert client.post("/api/meetings", json=url).status_code == 401
        resp = client.post("/api/meetings", json={"url": "https://example.com/x"}, headers=ADMIN)
        assert resp.status_code == 400
        # Anahtar yoksa anlaşılır hata (500 değil).
        missing = client.post("/api/meetings", json=url, headers=ADMIN)
        assert missing.status_code == 503 and "SONIOX_API_KEY" in missing.json()["detail"]


def test_join_refused_when_concurrency_or_daily_limit_hit(monkeypatch) -> None:
    from katip.bot import supervisor as sup
    from katip.store import repo

    started: list[str] = []
    monkeypatch.setenv("SONIOX_API_KEY", "test")
    from katip.config import get_settings

    get_settings.cache_clear()

    async def fake_start(session_id, url, max_minutes=None):
        started.append(session_id)
        sup.supervisor._procs[session_id] = object()  # çalışan bot gibi görünsün

    monkeypatch.setattr(sup.supervisor, "start", fake_start)
    monkeypatch.setattr(sup.supervisor, "_procs", {})
    client = TestClient(app)
    body = {"url": "https://meet.google.com/abc-defg-hij"}
    with client:
        assert client.post("/api/meetings", json=body, headers=ADMIN).status_code == 200
        second = client.post("/api/meetings", json=body, headers=ADMIN)
        assert second.status_code == 429 and "en fazla 1 bot" in second.json()["detail"]

        sup.supervisor._procs.clear()

        async def lots_of_minutes() -> float:
            return 10_000.0

        monkeypatch.setattr(repo, "meeting_minutes_today", lots_of_minutes)
        third = client.post("/api/meetings", json=body, headers=ADMIN)
        assert third.status_code == 429 and "günlük" in third.json()["detail"]
    assert len(started) == 1


class StubWakeSTT(STTProvider):
    """Birisi 'Katip, not al: …' diyor."""

    translates = True

    async def stream(
        self, audio: AsyncIterator[bytes], *, language: str, target_language: str | None = None
    ) -> AsyncIterator[Utterance]:
        count = 0
        async for _ in audio:
            count += 1
            if count == 2:
                yield Utterance(
                    "w1", "Katip, not al: bütçe onaylandı", True, 0.0, 2.0, "1", language
                )


def _next_bot_message(bot, kind: str, limit: int = 10) -> dict:
    for _ in range(limit):
        msg = bot.receive_json()
        if msg.get("kind") == kind:
            return msg
    raise AssertionError(f"{kind} gelmedi")


def test_voice_command_reply_is_sent_to_meeting_chat(monkeypatch) -> None:
    _use(monkeypatch, StubWakeSTT)
    client = TestClient(app)
    sid = "s-wake"
    session_mod.registry.create(sid)

    with client, _bot(client, sid) as bot:
        for seq in range(3):
            bot.send_bytes(_frame(seq))
        reply = _next_bot_message(bot, "chat_send")
        assert reply["text"] == "Not alındı."

        # Toplantı chat'inden gelen /komut da işlenir; botun kendi mesajı yok sayılır.
        bot.send_text(json.dumps({"kind": "chat", "author": "Katip", "text": "/not yok say"}))
        bot.send_text(json.dumps({"kind": "chat", "author": "Ayşe", "text": "/not demo 3 Ekim"}))
        reply = _next_bot_message(bot, "chat_send")
        assert reply["text"] == "Not alındı."


def test_panel_found_from_working_directory(monkeypatch, tmp_path) -> None:
    """Docker'da paket site-packages'ta; panel çalışma dizinindeki web/panel'den bulunmalı."""
    from katip.api import main

    (tmp_path / "web" / "panel").mkdir(parents=True)
    (tmp_path / "web" / "panel" / "index.html").write_text("<html>panel</html>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        main, "__file__", "/usr/local/lib/python3.12/site-packages/katip/api/main.py"
    )
    assert main._find_panel_dir() == tmp_path / "web" / "panel"

    monkeypatch.setenv("KATIP_PANEL_DIR", str(tmp_path / "yok"))
    assert main._find_panel_dir() == tmp_path / "web" / "panel"  # geçersiz ayar yok sayılır


async def test_translation_marked_final_when_segment_finalizes() -> None:
    """Canlı testte çeviriler panelde kısmi (italik) kalıyordu."""
    from katip.pipeline.stt.base import Utterance

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    s = session_mod.MeetingSession("trfinal", stt=NoSTT(), persist=False, voice=False)
    q = s.subscribe()
    s._on_utterance(Utterance("u", "Merhaba", False, 0, 1, "1", "tr", "Hello", "en"))
    s._on_utterance(Utterance("u", "Merhaba", True, 0, 1, "1", "tr", "Hello", "en"))
    events = [q.get_nowait() for _ in range(q.qsize())]
    translations = [e.translation for e in events if e.kind == "translation"]
    assert [tr.is_final for tr in translations] == [False, True]


def test_panel_does_not_create_sessions_and_shows_history(monkeypatch) -> None:
    import asyncio as _asyncio

    from katip.events import Segment
    from katip.store import repo

    client = TestClient(app)
    with client:
        with _panel(client, "yok-boyle-bir-oturum") as panel:
            assert panel.receive_json()["text"] == "oturum bulunamadı"
        assert session_mod.registry.get("yok-boyle-bir-oturum") is None  # hayalet yok

        async def seed():
            await repo.upsert_meeting("eski", target_lang="en")
            await repo.save_segment(
                "eski",
                Segment(
                    id="e1",
                    session_id="eski",
                    speaker="Selin",
                    start_s=0,
                    end_s=2,
                    text="Merhaba",
                    lang="tr",
                    is_final=True,
                    translation="Hello",
                ),
            )

        client.portal.call(seed)
        with _panel(client, "eski") as panel:
            kinds = [panel.receive_json() for _ in range(3)]
        assert kinds[0]["segment"]["text"] == "Merhaba"
        assert kinds[1]["translation"]["text"] == "Hello"
        assert kinds[2]["state"] == "ended"
        assert session_mod.registry.get("eski") is None
        _ = _asyncio


def test_ingest_panel_and_reads_require_credentials(monkeypatch, api_client) -> None:
    """Güvenlik: bot anahtarı olmadan ses/komut gönderilemez; imzasız panel ve okuma kapalı."""
    from starlette.websockets import WebSocketDisconnect

    _use(monkeypatch, StubSoniox)
    client = api_client
    sid = "s-guard"
    session_mod.registry.create(sid)
    security.issue_bot_token(sid)
    for headers in ({}, {"X-Katip-Bot-Token": "yanlis"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/ingest/{sid}", headers=headers) as ws:
                ws.receive_text()
    # Başka toplantının imzası bu toplantıyı açmaz; imzasız da açılmaz.
    other = security.view_link_params("baska")
    for query in ("", f"?{other}"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/panel/{sid}{query}") as ws:
                ws.receive_text()
    assert client.get(f"/api/meetings/{sid}/transcript").status_code == 401
    assert client.get(f"/api/meetings/{sid}/transcript?{other}").status_code == 401
    ok = client.get(f"/api/meetings/{sid}/transcript?{security.view_link_params(sid)}")
    assert ok.status_code == 200
    assert client.get("/api/meetings").status_code == 401
    assert client.get("/api/people").status_code == 401


def test_signed_links_expire() -> None:
    token = security.sign("m1", ttl_s=-1)
    assert not security.verify("m1", token)
    assert security.verify("m1", security.sign("m1", ttl_s=60))
    assert not security.verify("m2", security.sign("m1", ttl_s=60))


def test_meeting_links_are_validated_strictly() -> None:
    from katip.bot.platforms.base import platform_for

    for bad in (
        "https://evil.example/?meet.google.com",
        "https://meet.google.com.evil.example/abc",
        "http://meet.google.com/abc-defg-hij",
        "https://user@meet.google.com/abc",
        "https://meet.google.com:8443/abc",
        "https://127.0.0.1/teams.microsoft.com",
        "javascript:alert(1)//teams.microsoft.com",
    ):
        with pytest.raises(ValueError):
            platform_for(bad)
    assert platform_for("https://meet.google.com/abc-defg-hij").__name__ == "GoogleMeet"
    assert platform_for("https://teams.microsoft.com/meet/1?p=x").__name__ == "MicrosoftTeams"
    assert platform_for("https://us05web.zoom.us/j/123").__name__ == "Zoom"


def test_bot_process_gets_no_secrets(monkeypatch) -> None:
    from katip.bot.supervisor import bot_environment

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-gizli")
    monkeypatch.setenv("SONIOX_API_KEY", "gizli")
    monkeypatch.setenv("KATIP_ADMIN_TOKEN", "gizli")
    monkeypatch.setenv("KATIP_DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("UNEXPECTED_AUTH", "gizli")
    monkeypatch.setenv("KATIP_M365_CLIENT_SECRET", "gizli")
    monkeypatch.setenv("KATIP_BOT_DISPLAY_NAME", "Katip")
    env = bot_environment("bot-anahtari")
    assert env["KATIP_BOT_TOKEN"] == "bot-anahtari"
    assert env["KATIP_BOT_DISPLAY_NAME"] == "Katip"
    for key in ("ANTHROPIC_API_KEY", "SONIOX_API_KEY", "KATIP_ADMIN_TOKEN", "KATIP_DATABASE_URL"):
        assert key not in env
    assert "UNEXPECTED_AUTH" not in env and "KATIP_M365_CLIENT_SECRET" not in env


async def test_leave_meeting_keeps_leave_task_referenced(monkeypatch) -> None:
    """'Katip, toplantıdan çık': ayrılma görevi döngüye ait kümede tutulur, hata vermez."""
    from katip.pipeline.stt.base import STTProvider

    class NoSTT(STTProvider):
        translates = False

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    left = []

    async def fake_leave(self):
        left.append(self.session_id)

    monkeypatch.setattr(session_mod.MeetingSession, "_leave", fake_leave)
    s = session_mod.MeetingSession("leave1", stt=NoSTT(), persist=False, voice=False)
    s.leave_meeting()
    tasks = session_mod._report_tasks.get()
    assert len(tasks) == 1
    await next(iter(tasks))
    assert left == ["leave1"] and not tasks
