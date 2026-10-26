"""Sesli cevap: Soniox TTS istemcisi (sahte sunucu), telaffuz sözlüğü, sesli/yazılı kararı."""

from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest
import websockets

from katip.config import get_settings
from katip.voice.tts import SonioxTTS, TTSError, _WavStripper, apply_lexicon


def wav_bytes(pcm: bytes, rate: int = 24000) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


@pytest.fixture
def tts_env(monkeypatch: pytest.MonkeyPatch):
    def configure(url: str) -> None:
        monkeypatch.setenv("SONIOX_API_KEY", "test-key")
        monkeypatch.setenv("KATIP_TTS_WS_URL", url)
        get_settings.cache_clear()

    yield configure
    get_settings.cache_clear()


async def test_streams_raw_pcm_and_sends_turkish_config(tts_env) -> None:
    seen: dict = {}
    pcm = b"\x01\x02" * 4800

    async def handler(ws) -> None:
        seen["authorization"] = ws.request.headers["Authorization"]
        seen["config"] = json.loads(await ws.recv())
        seen["text"] = json.loads(await ws.recv())
        sid = seen["config"]["stream_id"]
        for part in (pcm[:4800], pcm[4800:]):
            await ws.send(json.dumps({"stream_id": sid, "audio": base64.b64encode(part).decode()}))
        await ws.send(json.dumps({"stream_id": sid, "audio": "", "audio_end": True}))
        await ws.send(json.dumps({"stream_id": sid, "terminated": True}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        tts_env(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        out = b"".join(
            [c async for c in SonioxTTS().synthesize("Merhaba Ayşe, not aldım.", language="tr")]
        )

    assert out == pcm
    cfg = seen["config"]
    assert seen["authorization"] == "Bearer test-key"
    assert "api_key" not in cfg
    assert cfg["return_timestamps"] is True
    assert (
        cfg["language"] == "tr" and cfg["audio_format"] == "pcm_s16le" and cfg["voice"] == "Rania"
    )
    assert seen["text"]["text"] == "Merhaba Ayşe, not aldım."  # Türkçe karakterler bozulmadan
    assert seen["text"]["text_end"] is True


async def test_falls_back_to_wav_when_raw_pcm_rejected(tts_env) -> None:
    pcm = b"\x10\x00" * 2000
    formats: list[str] = []

    async def handler(ws) -> None:
        cfg = json.loads(await ws.recv())
        await ws.recv()
        formats.append(cfg["audio_format"])
        if cfg["audio_format"] == "pcm_s16le":
            await ws.send(
                json.dumps({"error_code": 400, "error_message": "unsupported audio_format"})
            )
            return
        data = wav_bytes(pcm)
        # Başlığı bilerek iki parçaya bölerek gönder.
        for part in (data[:30], data[30:]):
            await ws.send(json.dumps({"audio": base64.b64encode(part).decode()}))
        await ws.send(json.dumps({"terminated": True}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        tts_env(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        tts = SonioxTTS()
        out = b"".join([c async for c in tts.synthesize("Tamam", language="tr")])

    assert formats == ["pcm_s16le", "wav"]
    assert out == pcm


async def test_other_errors_raise(tts_env) -> None:
    async def handler(ws) -> None:
        await ws.recv()
        await ws.recv()
        await ws.send(json.dumps({"error_code": 402, "error_message": "budget exhausted"}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        tts_env(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        with pytest.raises(TTSError, match="402"):
            _ = [c async for c in SonioxTTS().synthesize("x", language="tr")]


async def test_stream_splits_long_text_and_exposes_timestamps_and_cancel(tts_env):
    seen = []
    timestamps = {
        "characters": list("Merhaba."),
        "character_start_times_seconds": [0.0] * 8,
        "character_end_times_seconds": [0.1] * 8,
    }
    cancelled = asyncio.Event()

    async def handler(ws):
        config = json.loads(await ws.recv())
        while True:
            message = json.loads(await ws.recv())
            seen.append(message)
            if message.get("text_end"):
                break
        await ws.send(
            json.dumps(
                {
                    "stream_id": config["stream_id"],
                    "audio": base64.b64encode(b"\0\1" * 2400).decode(),
                    "timestamps": timestamps,
                }
            )
        )
        seen.append(json.loads(await ws.recv()))
        cancelled.set()
        await ws.send(json.dumps({"stream_id": config["stream_id"], "terminated": True}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        tts_env(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        async with SonioxTTS().stream(language="tr") as stream:
            await stream.send("a" * 10001, end=True)
            iterator = aiter(stream)
            event = await anext(iterator)
            assert event.audio == b"\0\1" * 2400
            assert event.timestamps == timestamps
            await stream.cancel()
            await stream.cancel()
            assert [item async for item in iterator] == []
        await asyncio.wait_for(cancelled.wait(), 1)
    assert [len(item["text"]) for item in seen[:-1]] == [5000, 5000, 1]
    assert [item["text_end"] for item in seen[:-1]] == [False, False, True]
    assert seen[-1] == {"stream_id": seen[0]["stream_id"], "cancel": True}
    assert len({item["stream_id"] for item in seen}) == 1


def test_wav_stripper_handles_split_header() -> None:
    pcm = bytes(range(200))
    data = wav_bytes(pcm)
    s = _WavStripper()
    out = b"".join(s.feed(data[i : i + 7]) for i in range(0, len(data), 7))
    assert out == pcm


def test_lexicon_keeps_turkish_suffixes() -> None:
    lex = {"Example": "Eksampıl", "API": "ey pi ay"}
    assert apply_lexicon("Example'in API anahtarı", lex) == "Eksampıl'in ey pi ay anahtarı"
    assert apply_lexicon("RAPID değişmez", lex) == "RAPID değişmez"  # kelime içi eşleşme yok


async def test_session_say_streams_audio_to_bot(monkeypatch) -> None:
    monkeypatch.setenv("KATIP_TTS_LEXICON", '{"Example":"Eksampıl"}')
    from katip.pipeline import session as session_mod
    from katip.pipeline.stt.base import STTProvider

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    class FakeTTS:
        sample_rate = 24000

        def __init__(self) -> None:
            self.texts: list[str] = []

        async def synthesize(self, text, *, language):
            self.texts.append(text)
            yield b"\x00\x01" * 6000  # 0.5 sn
            yield b"\x00\x02" * 100

    fake = FakeTTS()
    monkeypatch.setattr(session_mod, "_build_tts", lambda: fake)
    s = session_mod.MeetingSession("tts", stt=NoSTT(), persist=False, voice=False)
    s.set_reply_mode("voice")

    s.say("Example raporu hazır.")
    await asyncio.sleep(0.05)
    for task in list(s._background):
        await task

    kinds = []
    while not s.outbox.empty():
        kinds.append(s.outbox.get_nowait())
    # Tamamen sesli: chat'e hiçbir şey yazılmaz.
    assert all(k["kind"] != "chat_send" for k in kinds)
    assert kinds[0] == {"kind": "speak_start", "sample_rate": 24000}
    chunks = [k for k in kinds if k["kind"] == "speak_chunk"]
    total = sum(len(base64.b64decode(c["pcm"])) for c in chunks)
    # 24 kHz 16-bit: 200 ms = 9600 bayt → bir tam paket + kalan
    assert total == 12000 + 200 and len(chunks) == 2
    assert kinds[-1] == {"kind": "speak_end"}
    assert fake.texts == ["Eksampıl raporu hazır."]  # telaffuz sözlüğü uygulandı


async def test_addressed_hard_stop_stops_speech_without_cancelling(monkeypatch) -> None:
    from katip.pipeline import session as session_mod
    from katip.pipeline.stt.base import STTProvider, Utterance

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    class SlowTTS:
        sample_rate = 24000

        async def synthesize(self, text, *, language):
            yield b"\x00\x01" * 4800
            await asyncio.Event().wait()

    monkeypatch.setattr(session_mod, "_build_tts", SlowTTS)
    s = session_mod.MeetingSession("barge", stt=NoSTT(), persist=False, voice=False)
    s.set_reply_mode("voice")
    started = asyncio.Event()
    send = s._send_audio

    async def sent(pcm, rate):
        await send(pcm, rate)
        started.set()

    monkeypatch.setattr(s, "_send_audio", sent)
    s.say("Uzun bir cevap veriyorum.")
    await asyncio.wait_for(started.wait(), 1)
    # Açık durma isteği anında keser; sunumun çalıştığı görev iptal edilmez.
    task = s._speak_task
    s._on_utterance(Utterance("x1", "Katip dur", False, 0.0, 0.3, "2", "tr"))
    await asyncio.wait_for(task, 0.2)

    kinds = []
    while not s.outbox.empty():
        kinds.append(s.outbox.get_nowait()["kind"])
    assert "speak_stop" in kinds and "speak_end" not in kinds, kinds
    assert task.done() and not task.cancelled()
    await s.shutdown()


async def test_chat_mode_never_speaks(monkeypatch) -> None:
    from katip.pipeline import session as session_mod
    from katip.pipeline.stt.base import STTProvider

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    def boom():
        raise AssertionError("yazılı modda TTS çağrılmamalı")

    monkeypatch.setattr(session_mod, "_build_tts", boom)
    s = session_mod.MeetingSession("chat", stt=NoSTT(), persist=False, voice=False)
    assert s.reply_mode == "chat"
    s.say("Uzun bir cevap. " * 40)
    await asyncio.sleep(0.01)
    msgs = [s.outbox.get_nowait() for _ in range(s.outbox.qsize())]
    assert [m["kind"] for m in msgs] == ["chat_send"]


async def test_voice_mode_falls_back_to_chat_when_tts_unavailable(monkeypatch) -> None:
    from katip.pipeline import session as session_mod
    from katip.pipeline.stt.base import STTProvider

    class NoSTT(STTProvider):
        translates = True

        async def stream(self, audio, *, language, target_language=None):
            if False:
                yield

    class BrokenTTS:
        sample_rate = 24000

        async def synthesize(self, text, *, language):
            raise TTSError(402, "budget exhausted")
            yield b""  # pragma: no cover

    monkeypatch.setattr(session_mod, "_build_tts", BrokenTTS)
    s = session_mod.MeetingSession("fb", stt=NoSTT(), persist=False, voice=False)
    s.set_reply_mode("voice")
    s.say("Not aldım.")
    await asyncio.sleep(0.05)
    for task in list(s._background):
        await task
    msgs = [s.outbox.get_nowait() for _ in range(s.outbox.qsize())]
    assert {"kind": "chat_send", "text": "Not aldım."} in msgs  # cevap kaybolmadı
