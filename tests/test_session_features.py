"""Oturum: kayıt dışı modu, ekran görüntüsü akışı ve botun gerçek ekran görüntüsü alması."""

from __future__ import annotations

import asyncio

import pytest

from katip.pipeline import session as session_mod
from katip.pipeline.stt.base import STTProvider, Utterance


class NoSTT(STTProvider):
    translates = True

    async def stream(self, audio, *, language, target_language=None):
        if False:
            yield


def make_session(name: str) -> session_mod.MeetingSession:
    return session_mod.MeetingSession(name, stt=NoSTT(), persist=False, voice=False)


def drain(s) -> list[dict]:
    return [s.outbox.get_nowait() for _ in range(s.outbox.qsize())]


async def settle(s) -> None:
    await asyncio.sleep(0.02)
    if s._final_worker:
        await s._final_queue.put(None)
        await s._final_worker


async def test_off_record_speech_is_not_kept_but_resume_works() -> None:
    s = make_session("offrec")
    s.set_paused(True)
    s._on_utterance(Utterance("a", "Maaş zammı yüzde otuz olacak", True, 0.0, 2.0, "1", "tr"))
    s._on_utterance(Utterance("b", "Katip, kayda devam", True, 2.5, 3.5, "1", "tr"))
    await settle(s)

    # Kayıt dışı konuşma hiçbir yerde tutulmadı (yalnızca Katip'in "kayda devam" cevabı var).
    assert [seg.speaker for seg in s.segments.values()] == ["Katip"]
    assert s.paused is False  # ama "kayda devam" komutu algılandı
    s._final_worker = None
    s._on_utterance(Utterance("c", "Bütçe onaylandı", True, 4.0, 5.0, "1", "tr"))
    # Kayıt dışı döneme ait geç bir güncelleme sonradan gelse de yok sayılır.
    s._on_utterance(
        Utterance(
            "a",
            "Maaş zammı yüzde otuz olacak",
            True,
            0.0,
            2.0,
            "1",
            "tr",
            translation="Raise will be thirty percent",
        )
    )
    assert [k for k in s.segments if not k.startswith("katip-")] == ["c"]


async def test_screenshot_round_trip_and_timeout(database, monkeypatch) -> None:
    # Dosya yazımı kalıcı silme kaydını denetlediği için depo da kurulmalıdır.
    s = make_session("shots")
    s.request_screenshot("Ayşe")
    request = next(m for m in drain(s) if m["kind"] == "screenshot")

    number = await s.on_screenshot(request["request_id"], b"\xff\xd8\xff fake jpeg")
    assert number == 1
    assert any("1. görüntü" in m.get("text", "") for m in drain(s))
    # Aynı isteğe ikinci yükleme ya da bilinmeyen istek reddedilir.
    assert await s.on_screenshot(request["request_id"], b"x") is None

    # Bot cevap vermezse kullanıcıya haber verilir.
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(fut, timeout):  # noqa: ASYNC109 — asyncio.wait_for imzası
        return await real_wait_for(fut, 0.05)

    monkeypatch.setattr(session_mod.asyncio, "wait_for", fast_wait_for)
    s.request_screenshot("Ayşe")
    drain(s)
    await asyncio.sleep(0.15)
    assert any("alamadım" in m.get("text", "") for m in drain(s))


async def test_bot_takes_real_screenshot_and_uploads(monkeypatch) -> None:
    from playwright.async_api import async_playwright

    from katip.bot import runner

    sent: dict = {}
    monkeypatch.setenv("KATIP_BOT_TOKEN", "bot-anahtari")

    class FakeClient:
        def __init__(self, *a, **kw) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content, headers):
            sent.update(url=url, content=content, headers=headers)

            class R:
                status_code = 200

            return R()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 800, "height": 450})
            await page.set_content("<h1>Toplantı ekranı</h1>")
            await runner._take_screenshot(page, "sess1", "req1")
            await browser.close()
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Chromium yok")
        raise

    assert sent["url"].endswith("/api/internal/sess1/screenshots/req1")
    assert sent["content"][:3] == b"\xff\xd8\xff"  # gerçek JPEG
    # Bot yönetici token'ı değil, toplantıya özel anahtar kullanır.
    assert sent["headers"]["X-Katip-Bot-Token"] == "bot-anahtari"
    assert "Authorization" not in sent["headers"]


async def test_bot_leaves_after_long_silence(monkeypatch) -> None:
    from katip.events import SessionState

    monkeypatch.setenv("KATIP_IDLE_LEAVE_MINUTES", "1")
    from katip.config import get_settings

    get_settings.cache_clear()
    s = make_session("idle")
    left = []
    monkeypatch.setattr(s, "leave_meeting", lambda: left.append(True))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(session_mod.asyncio, "sleep", lambda *_: real_sleep(0))
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: 10_000.0)

    s.state = SessionState.ACTIVE
    s._last_speech = 10_000.0 - 61  # 61 sn sessizlik (limit 60 sn)
    await s._idle_watchdog()
    assert left == [True]
    assert any("kimse konuşmuyor" in m.get("text", "") for m in drain(s))


async def test_katip_replies_are_part_of_the_transcript(database) -> None:
    s = make_session("replies")
    s._on_utterance(Utterance("a", "Katip, not al: demo 3 Ekim", True, 0.0, 2.0, "1", "tr"))
    await settle(s)
    lines = s.transcript_text().splitlines()
    assert lines[-1] == "Katip: Not alındı."
    assert [m["text"] for m in drain(s) if m["kind"] == "chat_send"] == ["Not alındı."]


async def test_voice_reply_is_spoken_when_someone_is_talking(monkeypatch) -> None:
    s = make_session("busy")
    s.set_reply_mode("voice")
    from katip.config import get_settings

    class TTS:
        sample_rate = 24000

        async def synthesize(self, text, *, language):
            yield b"\0\0" * 240

    monkeypatch.setattr(session_mod, "_build_tts", TTS)
    monkeypatch.setattr(get_settings(), "voice_floor_wait_s", 0.02)
    monkeypatch.setattr(s, "channel_busy", lambda: True)
    sent_at = []
    original_send = s._send_audio

    async def send(pcm, rate):
        sent_at.append(asyncio.get_running_loop().time())
        await original_send(pcm, rate)

    monkeypatch.setattr(s, "_send_audio", send)
    # Başka biri konuşuyor (kesinleşmemiş metin akıyor).
    s._on_utterance(Utterance("x", "şimdi bütçe tarafında", False, 0.0, 1.0, "2", "tr"))
    started = asyncio.get_running_loop().time()
    assert await s._speak("Not alındı.")
    out = drain(s)
    assert not any(m["kind"] == "chat_send" for m in out)
    assert out[0]["kind"] == "speak_start" and out[-1]["kind"] == "speak_end"
    assert sent_at[0] - started <= get_settings().voice_floor_wait_s + 0.1
    await s.shutdown()


async def test_roster_skips_bot_and_lists_participants() -> None:
    from katip.events import RosterUpdate

    s = make_session("roster")
    s.update_roster(RosterUpdate(participants=["Ayşe Yılmaz", "Katip"], active_speakers=["Katip"]))
    assert s._roster == ["Ayşe Yılmaz"]
    assert s.participant_lines() == ["Ayşe Yılmaz"]


async def test_reply_language_tag_sets_tts_language_and_skips_turkish_normalizer(
    monkeypatch,
) -> None:
    s = make_session("lang")
    s.set_reply_mode("voice")
    seen = {}

    class FakeTTS:
        sample_rate = 16000

        async def synthesize(self, text, *, language):
            seen.update(text=text, language=language)
            yield b"\x00\x00" * 320

    monkeypatch.setattr(session_mod, "_build_tts", FakeTTS)
    s.say("[en] Our delivery deadline is October 10th.")
    await asyncio.sleep(0.05)
    assert seen == {"text": "Our delivery deadline is October 10th.", "language": "en"}
    assert s.transcript_text().endswith("Katip: Our delivery deadline is October 10th.")


async def test_privacy_notice_is_posted_to_chat_on_join(monkeypatch) -> None:
    from katip.events import SessionState

    real_sleep = asyncio.sleep
    monkeypatch.setattr(session_mod.asyncio, "sleep", lambda *_: real_sleep(0))
    s = make_session("notice")
    s.set_reply_mode("voice")  # sesli modda bile aydınlatma chat'e yazılır
    s.set_state(SessionState.ACTIVE)
    await real_sleep(0.05)
    chats = [m["text"] for m in drain(s) if m["kind"] == "chat_send"]
    assert chats and chats[0].startswith("Bilgilendirme (KVKK aydınlatma metni)")
    assert chats[0].endswith(" adresinde.")


async def test_report_chat_links_have_trailing_text(monkeypatch):
    import re
    from unittest.mock import AsyncMock

    from katip.config import get_settings
    from katip.events import SessionState
    from katip.reports import service

    session = make_session("report-links")
    session.state = SessionState.ACTIVE
    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://katip.example")
    monkeypatch.setattr(settings, "auto_report", True)
    monkeypatch.setattr(settings, "post_report_to_chat", True)
    monkeypatch.setattr(session, "_leave", AsyncMock())
    monkeypatch.setattr(service, "generate_report", AsyncMock(return_value=["report.pdf"]))
    session.leave_meeting()
    await session._report(interim=True)
    chats = [m["text"] for m in drain(session) if m["kind"] == "chat_send"]
    assert len(chats) == 2
    for text in chats:
        links = re.findall(r"https?://\S+", text)
        assert len(links) == 2
        assert all(link + " adresinde." in text for link in links)
    await asyncio.sleep(0)
    await session.shutdown()


async def test_voice_is_only_processed_for_consenting_candidates() -> None:
    from katip.events import RosterUpdate
    from katip.voice.identity import KnownVoice, VoiceIdentifier

    s = make_session("gate")
    s.identifier = VoiceIdentifier(object(), 16000, known=[KnownVoice(7, "Deniz", "member", None)])
    s.update_roster(RosterUpdate(participants=["Ayşe Yılmaz", "Mehmet Kaya"]))
    assert s._segment_candidates("1") == set()  # rızalı kimse toplantıda yok: iz çıkarılmaz
    s.update_roster(RosterUpdate(participants=["Deniz Kaya", "Ayşe Yılmaz"]))
    assert s._segment_candidates("1") == {7}  # yalnız toplantıdaki rızalı kişiyle karşılaştırılır


async def test_agent_transcript_uses_local_wall_clock(database) -> None:
    import re as _re

    s = make_session("clock")
    s._on_utterance(Utterance("a", "Bütçe onaylandı", True, 0.0, 2.0, "1", "tr"))
    line = s.transcript_text(timestamps=True).splitlines()[0]
    assert _re.fullmatch(r"\[\d{2}:\d{2}:\d{2}\] .+: Bütçe onaylandı", line)


async def test_own_chat_message_read_back_is_ignored(database) -> None:
    s = make_session("echo")
    s._chat("Toplantı notları:\nNot #6: Katip'in seslendirmesi")
    s.on_chat("Katip (Guest)", "başka bir şey")  # yazar botun kendisi
    s.on_chat("Ayşe", "Toplantı notları: Not #6: Katip'in seslendirmesi")  # satır sonu değişmiş
    assert s._final_worker is None  # hiçbiri işlenmek üzere kuyruğa girmedi
    s.on_chat("Ayşe", "Katip, evet")
    assert s._final_worker is not None


async def test_leave_waits_for_farewell_to_finish(monkeypatch) -> None:
    from katip.bot import supervisor as sup

    s = make_session("bye")
    stopped = []

    async def fake_stop(sid):
        stopped.append(session_mod.time.monotonic())

    monkeypatch.setattr(sup.supervisor, "stop", fake_stop)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(session_mod.asyncio, "sleep", lambda *_: real_sleep(0.01))
    s._speech_until = session_mod.time.monotonic() + 0.3  # veda cümlesi hâlâ çalınıyor
    await s._leave()
    assert stopped and stopped[0] >= s._speech_until


def make_mode_session(name: str, mode: str) -> session_mod.MeetingSession:
    return session_mod.MeetingSession(name, stt=NoSTT(), persist=False, voice=False, mode=mode)


async def test_record_mode_is_silent_and_untranslated() -> None:
    s = make_mode_session("kayit1", "kayit")
    assert s.target_lang is None and s.reply_mode == "chat" and s.max_minutes == 600
    with pytest.raises(ValueError):
        s.set_reply_mode("voice")
    s.say("Merhaba", channel="voice")  # sessiz kayıtta ses yok: chat'e gider
    out = drain(s)
    assert [m["kind"] for m in out] == ["chat_send"]
    full = make_mode_session("tam1", "tam")
    assert full.target_lang == "en" and full.max_minutes == 90


async def test_watchdog_rotates_stt_before_soniox_limit(monkeypatch) -> None:
    s = make_mode_session("rot", "kayit")
    s._stt_task = asyncio.get_running_loop().create_future()  # çalışan akış gibi
    now = session_mod.time.monotonic()
    s._last_speech = now
    s._stream_started_at = now - 281 * 60
    assert s._watchdog_tick(now) is False
    assert s._restart_stt is True and s._audio.get_nowait() is None
    s._stt_task.cancel()


async def test_leaves_when_alone_and_silent(monkeypatch) -> None:
    from katip.events import RosterUpdate

    s = make_mode_session("alone", "tam")
    left = []
    monkeypatch.setattr(s, "leave_meeting", lambda: left.append(True))
    s.update_roster(RosterUpdate(participants=["Ayşe"]))
    s.update_roster(RosterUpdate(participants=["Katip"]))  # herkes çıktı
    now = session_mod.time.monotonic()
    s._last_speech = now - 6 * 60
    assert s._watchdog_tick(now) is False  # yalnızlık yeni başladı
    s._alone_since = now - 6 * 60
    assert s._watchdog_tick(now) is True and left == [True]


async def test_labels_and_times_stay_consistent_across_stt_streams() -> None:
    s = make_session("streams")
    s._first_stream_at = 1000.0
    s._stream_no, s._stream_offset = 1, 0.0
    s._on_utterance(Utterance("a", "Birinci akış", True, 5.0, 6.0, "1", "tr"))
    s._on_utterance(Utterance("b", "Diğer kişi", True, 7.0, 8.0, "2", "tr"))
    # Soniox akışı yenilendi: etiketler yine "1"den başlıyor, zamanlar sıfırlanıyor.
    s._stream_no, s._stream_offset = 2, 300.0
    s._on_utterance(Utterance("c", "İkinci akış", True, 1.0, 2.0, "1", "tr"))
    segs = s.final_segments()
    assert [x.text for x in segs] == ["Birinci akış", "Diğer kişi", "İkinci akış"]  # sıralı
    assert segs[2].start_s == 301.0 and segs[2].speaker_label == "3"  # yeni kişi sayılır
    assert segs[0].speaker_label == "1"


async def test_self_introduction_makes_speaker_a_verification_candidate() -> None:
    from katip.events import RosterUpdate
    from katip.voice.identity import KnownVoice, VoiceIdentifier

    s = make_session("claim")
    s.identifier = VoiceIdentifier(
        object(), 16000, known=[KnownVoice(1, "Deniz Kaya", "admin", None)]
    )
    # Deniz başkasının cihazından konuşuyor: katılımcı listesinde yok.
    s.update_roster(RosterUpdate(participants=["Mehmet Demir"]))
    assert s._segment_candidates("1") == set()
    s.claim_name("1", "Ankara")  # kişi değil: yok sayılır
    assert s._segment_candidates("1") == set()
    s.claim_name("1", "Deniz")
    assert s._segment_candidates("1") == {1}


async def test_each_sentence_gets_its_own_speaker_even_with_one_label() -> None:
    """Soniox iki kişiyi aynı etikete düşürse de cümleler kendi sahibine yazılır (9. test)."""
    import numpy as np
    from test_voice import SR, FakeEncoder, voice_of

    from katip.events import RosterUpdate
    from katip.voice.identity import KnownVoice, VoiceIdentifier

    s = make_session("merged")
    s.identifier = VoiceIdentifier(
        FakeEncoder(),
        SR,
        known=[
            KnownVoice(1, "Deniz Kaya", "admin", voice_of(3)),
            KnownVoice(2, "Mehmet Demir", "member", voice_of(5)),
        ],
    )
    # Aynı cihaz: ekran hep Murat'ı gösteriyor, katılımcı listesinde ikisi de var.
    s.update_roster(RosterUpdate(participants=["Mehmet Demir", "Deniz Kaya"]))
    amp = {0.0: 3, 4.0: 5, 8.0: 3}  # Deniz, Murat, Deniz
    s._ring.slice_stream = lambda t0, t1: np.full(int((t1 - t0) * SR), amp[round(t0)], np.int16)
    s._on_utterance(Utterance("a", "Ben anlatıyorum", True, 0.0, 3.0, "1", "tr"))
    s._on_utterance(Utterance("b", "Tamam abi", True, 4.0, 7.0, "1", "tr"))
    s._on_utterance(Utterance("c", "Devam ediyorum", True, 8.0, 11.0, "1", "tr"))
    await settle(s)
    names = [x.speaker for x in s.final_segments() if x.speaker != "Katip"]
    assert names == ["Deniz Kaya", "Mehmet Demir", "Deniz Kaya"]
    # Her kişi toplantıda yalnız bir kez selamlanır.
    greetings = [x.text for x in s.final_segments() if x.speaker == "Katip"]
    assert len(greetings) == 2


async def test_merged_label_is_not_renamed_by_a_single_tile() -> None:
    from katip.events import RosterUpdate

    s = make_session("tiles")
    s._first_stream_at = 1000.0
    s._stream_no = 1
    s._speakers.observe(["Ayşe Yılmaz"], at=1000.0)
    s._speakers.observe(["Mehmet Kaya"], at=1005.0)
    s.update_roster(RosterUpdate(participants=["Ayşe Yılmaz", "Mehmet Kaya"]))
    s._on_utterance(Utterance("a", "Bütçe hazır", True, 1.0, 4.0, "1", "tr"))
    s._on_utterance(Utterance("b", "Sunumu ben yaparım", True, 6.0, 9.0, "1", "tr"))  # aynı etiket
    await settle(s)
    assert [x.speaker for x in s.final_segments()] == ["Konuşmacı 1", "Konuşmacı 1"]


async def test_final_translation_does_not_vote_twice_or_rename_speech():
    s = make_session("duplicate-final")
    s._first_stream_at = 1000.0
    s._stream_no = 1
    for at in range(10):
        s._speakers.observe(["Ayşe"], at=1000 + at)
    first = Utterance("a", "Bütçe hazır", True, 1.0, 3.0, "1", "tr")
    s._on_utterance(first)
    for _ in range(10):
        s._on_utterance(
            Utterance("a", "Bütçe hazır", True, 1.0, 3.0, "1", "tr", translation="Ready")
        )
    assert not s._speakers._samples
    assert s.segments["a"].speaker == "Konuşmacı 1"
    await settle(s)


async def test_conflicting_dom_cannot_override_stable_label_mapping():
    s = make_session("conflicting-dom")
    for at in range(10):
        s._speakers.observe(["Deniz"], at=at)
    for at in (1, 4, 7):
        s._speakers.learn("1", at, at + 2)
    assert s._display_name("1", "Deniz") == "Konuşmacı 1"
    assert s._display_name("1", "Ayşe") == "Konuşmacı 1"
    await settle(s)


async def test_single_visible_account_is_not_speaker_identity():
    from katip.events import RosterUpdate

    s = make_session("single-account")
    s.update_roster(RosterUpdate(participants=["ase"], active_speakers=["Not in roster"]))
    assert s.speaker_name("1") == "Konuşmacı 1"
    assert s._speakers.name_at(s._stream_started_at, s._stream_started_at + 1) is None
    await settle(s)


async def test_agent_uses_ambiguous_sentence_instead_of_previous_label_candidate():
    s = make_session("agent-attribution")
    s._first_stream_at = 1000
    s._stream_no = 1
    for at in range(1000, 1021):
        s._speakers.observe(["Deniz"], at=at)
    for at in (1001, 1004, 1007, 1010, 1013, 1016):
        s._speakers.learn("1", at, at + 2)
    for at in range(1021, 1025):
        s._speakers.observe(["Ayşe"], at=at)
    s._on_utterance(Utterance("conflict", "Bütçe hazır", True, 22, 24, "1", "tr"))
    assert s._speakers.name_for("1") == "Deniz"
    assert s.segments["conflict"].speaker == s.speaker_name("1") == "Konuşmacı 1"
    await settle(s)


async def test_interim_report_not_repeated_without_new_speech(monkeypatch) -> None:
    s = make_session("interim")
    calls = []

    async def fake_report(*, interim=False):
        calls.append(interim)

    monkeypatch.setattr(s, "_report", fake_report)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(session_mod.asyncio, "sleep", lambda *_: real_sleep(0))
    s.generate_report_now()
    s.generate_report_now()  # hemen ardından, yeni konuşma yok: atlanır
    await real_sleep(0.01)
    assert calls == [True]
    s._on_utterance(Utterance("x", "Yeni bir konu", True, 0.0, 2.0, "1", "tr"))
    s.generate_report_now()  # yeni konuşma var: üretilir
    await real_sleep(0.01)
    assert calls == [True, True]


async def test_account_binding_does_not_follow_acoustic_labels():
    from katip.identity import ParticipantIdentity

    s = make_session("email-attribution")
    first = ParticipantIdentity(name="Deniz", email="deniz@example.com")
    second = ParticipantIdentity(name="Mehmet", email="mehmet@example.com")
    s._identities = {first.email: (first, "panel"), second.email: (second, "panel")}
    s._participants = {"Deniz": first.email, "Mehmet": second.email}
    assert s._display_name("1", "Deniz") == "Deniz"
    assert s._display_name("1", "Mehmet") == "Mehmet"
    assert s._display_name("2", "Deniz") == "Deniz"
    assert s._display_name("1") == "Konuşmacı 1"
    s._identities[second.email] = (second, "calendar")
    assert s._display_name("1", "Mehmet") == "Konuşmacı 1"
    await settle(s)
