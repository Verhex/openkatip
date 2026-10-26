"""Soniox adaptörü: token birleştirici (saf mantık) + sahte Soniox sunucusuyla ağ testi."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import websockets

from katip.config import get_settings
from katip.pipeline.stt.soniox import SonioxAssembler, SonioxError, SonioxSTT


def tok(text, *, final=True, start=None, end=None, speaker="1", status="original"):
    t = {"text": text, "is_final": final, "translation_status": status}
    if status != "translation":
        t["speaker"] = speaker
        t["language"] = "tr"
        if start is not None:
            t["start_ms"] = start
            t["end_ms"] = end if end is not None else start + 200
    return t


def tr(text, *, final=True):
    return tok(text, final=final, status="translation")


END = {"text": "<end>", "is_final": True}


# ------------------------------------------------------------------ birleştirici


def test_partial_then_final_keeps_same_id() -> None:
    a = SonioxAssembler("tr", "en")

    first = a.feed({"tokens": [tok("Mer", final=False, start=0)]})
    assert len(first) == 1 and not first[0].is_final and first[0].text == "Mer"

    second = a.feed(
        {"tokens": [tok("Merhaba", start=0, end=400), tok(" ekip", final=False, start=450)]}
    )
    assert second[-1].id == first[0].id
    assert second[-1].text == "Merhaba ekip"
    assert not second[-1].is_final

    done = a.feed({"tokens": [tok(" ekip", start=450, end=800), END]})
    finals = [u for u in done if u.is_final]
    assert len(finals) == 1
    assert finals[0].id == first[0].id
    assert finals[0].text == "Merhaba ekip"
    assert finals[0].start_s == 0.0 and finals[0].end_s == 0.8
    assert finals[0].speaker_label == "1"


def test_translation_in_same_segment() -> None:
    a = SonioxAssembler("tr", "en")
    a.feed({"tokens": [tok("Merhaba", start=0), tr("Hel", final=False)]})
    out = a.feed({"tokens": [tr("Hello"), END]})
    final = [u for u in out if u.is_final][0]
    assert final.text == "Merhaba"
    assert final.translation == "Hello"
    assert final.translation_lang == "en"


def test_late_translation_attaches_to_previous_segment() -> None:
    a = SonioxAssembler("tr", "en")
    closed = [
        u for u in a.feed({"tokens": [tok("Bugün toplantı var", start=0), END]}) if u.is_final
    ][0]
    assert closed.translation == ""

    # <end>'den sonra gelen final çeviri: yeni konuşma başlamadığı için önceki parçaya ait.
    late = a.feed({"tokens": [tr("There is a meeting"), tr(" today")]})
    assert [u.id for u in late] == [closed.id]
    assert late[0].is_final
    assert late[0].translation == "There is a meeting today"

    # Yeni orijinal konuşma başlayınca sonraki çeviriler yeni parçaya gider.
    nxt = a.feed({"tokens": [tok("Evet", start=3000), tr("Yes"), END]})
    final = [u for u in nxt if u.is_final][-1]
    assert final.id != closed.id
    assert final.text == "Evet" and final.translation == "Yes"


def test_late_translation_flushed_when_new_speech_arrives_in_same_message() -> None:
    a = SonioxAssembler("tr", "en")
    closed = [u for u in a.feed({"tokens": [tok("Tamam", start=0), END]}) if u.is_final][0]
    out = a.feed({"tokens": [tr("Okay"), tok("Devam", start=2000)]})
    prev_updates = [u for u in out if u.id == closed.id]
    assert prev_updates and prev_updates[-1].translation == "Okay"


def test_speaker_change_splits_segment() -> None:
    a = SonioxAssembler("tr", None)
    out = a.feed(
        {
            "tokens": [
                tok("Ben hazırım", start=0, speaker="1"),
                tok(" ben de", start=900, speaker="2"),
                END,
            ]
        }
    )
    finals = [u for u in out if u.is_final]
    assert [(u.speaker_label, u.text) for u in finals] == [("1", "Ben hazırım"), ("2", "ben de")]
    assert finals[0].id != finals[1].id


def test_duplicate_partials_are_suppressed() -> None:
    a = SonioxAssembler("tr", None)
    assert a.feed({"tokens": [tok("Selam", final=False, start=0)]})
    assert a.feed({"tokens": [tok("Selam", final=False, start=0)]}) == []


def test_flush_finalizes_pending_tokens() -> None:
    a = SonioxAssembler("tr", None)
    a.feed({"tokens": [tok("yarım kalan", start=0)]})
    out = a.flush()
    assert len(out) == 1 and out[0].is_final and out[0].text == "yarım kalan"


# ------------------------------------------------------------------- ağ testi


@pytest.fixture
def soniox_env(monkeypatch: pytest.MonkeyPatch):
    def configure(url: str) -> None:
        monkeypatch.setenv("SONIOX_API_KEY", "test-key")
        monkeypatch.setenv("KATIP_SONIOX_WS_URL", url)
        get_settings.cache_clear()

    yield configure
    get_settings.cache_clear()


async def _audio(frames: int) -> AsyncIterator[bytes]:
    for _ in range(frames):
        yield b"\x00\x00" * 320
        await asyncio.sleep(0)


async def test_stream_against_fake_soniox_server(soniox_env) -> None:
    received: dict = {"config": None, "audio_frames": 0, "eof": False}

    async def handler(ws) -> None:
        received["config"] = json.loads(await ws.recv())
        async for msg in ws:
            if msg == "":  # ses sonu: boş metin frame'i (gerçek API davranışı)
                received["eof"] = True
                break
            if isinstance(msg, bytes):
                received["audio_frames"] += 1
        await ws.send(json.dumps({"tokens": [tok("Merhaba", start=0, end=500), tr("Hello")]}))
        await ws.send(json.dumps({"tokens": [END]}))
        await ws.send(json.dumps({"tokens": [], "finished": True}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        soniox_env(f"ws://127.0.0.1:{port}")

        stt = SonioxSTT()
        utterances = [u async for u in stt.stream(_audio(5), language="tr", target_language="en")]

    cfg = received["config"]
    assert cfg["api_key"] == "test-key"
    assert cfg["model"] == "stt-rt-v5"
    assert cfg["audio_format"] == "pcm_s16le"
    assert cfg["sample_rate"] == 16000 and cfg["num_channels"] == 1
    assert cfg["language_hints"] == ["tr"]
    assert cfg["enable_speaker_diarization"] is True
    assert cfg["translation"] == {"type": "one_way", "target_language": "en"}
    assert received["audio_frames"] == 5
    assert received["eof"] is True

    final = [u for u in utterances if u.is_final]
    assert len(final) == 1
    assert final[0].text == "Merhaba" and final[0].translation == "Hello"


async def test_no_translation_config_when_same_language(soniox_env) -> None:
    seen: dict = {}

    async def handler(ws) -> None:
        seen["config"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"tokens": [], "finished": True}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        soniox_env(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        _ = [u async for u in SonioxSTT().stream(_audio(1), language="tr", target_language="tr")]

    assert "translation" not in seen["config"]


async def test_error_message_raises(soniox_env) -> None:
    async def handler(ws) -> None:
        await ws.recv()
        await ws.send(json.dumps({"error_code": 401, "error_message": "Invalid API key"}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        soniox_env(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        with pytest.raises(SonioxError, match="401"):
            _ = [u async for u in SonioxSTT().stream(_audio(50), language="tr")]


def test_eu_region_switches_soniox_endpoints(monkeypatch) -> None:
    from katip.config import get_settings

    s = get_settings()
    assert s.soniox_endpoint(s.soniox_ws_url) == "wss://stt-rt.soniox.com/transcribe-websocket"
    monkeypatch.setenv("KATIP_SONIOX_REGION", "eu")
    get_settings.cache_clear()
    s = get_settings()
    assert s.soniox_endpoint(s.soniox_ws_url) == "wss://stt-rt.eu.soniox.com/transcribe-websocket"
    assert s.soniox_endpoint(s.tts_ws_url) == "wss://tts-rt.eu.soniox.com/tts-websocket"
