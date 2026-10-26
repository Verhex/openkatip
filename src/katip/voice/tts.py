"""Soniox gerçek zamanlı TTS istemcisi (ham WebSocket).

Sözleşme (https://soniox.com/docs/api-reference/tts/websocket-api):
  1. wss://tts-rt.soniox.com/tts-websocket'e bağlan.
  2. Kimlik bağlantı başlığında; config: stream_id, model, language, voice, biçim, zaman damgası.
  3. Metni {"text", "text_end", "stream_id"} mesajlarıyla gönder; sonuncusu text_end=true.
  4. Sunucu {"audio": <base64>, "audio_end": bool} parçaları, en sonda {"terminated": true} döner.

Çıktı her zaman ham 16-bit PCM (mono) olarak verilir. Önce `pcm_s16le` istenir; sağlayıcı
bu biçimi reddederse `wav` istenip RIFF başlığı ayıklanır — belgelerde ham PCM'in desteklendiği
açıkça yazmadığı için iki yol da hazır.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import websockets

from katip.config import Settings, get_settings
from katip.logging import get_logger

log = get_logger(__name__)


class TTSError(RuntimeError):
    def __init__(self, code, message: str) -> None:
        super().__init__(f"Soniox TTS hatası {code}: {message}")
        self.code = code


class _WavStripper:
    """Akan WAV baytlarından RIFF/fmt başlığını atıp yalnızca PCM verisini geçirir."""

    def __init__(self) -> None:
        self._head = b""
        self._done = False

    def feed(self, data: bytes) -> bytes:
        if self._done:
            return data
        self._head += data
        if len(self._head) < 12:
            return b""
        if self._head[:4] != b"RIFF":
            self._done = True  # başlık yok: zaten ham veri
            out, self._head = self._head, b""
            return out
        pos = 12
        while pos + 8 <= len(self._head):
            chunk_id = self._head[pos : pos + 4]
            (size,) = struct.unpack("<I", self._head[pos + 4 : pos + 8])
            if chunk_id == b"data":
                self._done = True
                out = self._head[pos + 8 :]
                self._head = b""
                return out
            pos += 8 + size
        return b""


class SonioxTTS:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.soniox_api_key:
            raise RuntimeError("SONIOX_API_KEY tanımlı değil")
        self._format = "pcm_s16le"

    @property
    def sample_rate(self) -> int:
        return self._settings.tts_sample_rate

    def stream(self, *, language: str) -> SonioxStream:
        return SonioxStream(self, language)

    async def synthesize(self, text: str, *, language: str) -> AsyncIterator[bytes]:
        """Metni sese çevirir; 16-bit mono PCM parçaları yayınlar."""
        async with self.stream(language=language) as stream:
            await stream.send(text, end=True)
            async for event in stream:
                if event.audio:
                    yield event.audio


@dataclass
class TTSEvent:
    audio: bytes
    timestamps: dict | None = None


class SonioxStream:
    def __init__(self, tts: SonioxTTS, language: str) -> None:
        self.tts = tts
        self.language = language
        self.stream_id = uuid.uuid4().hex[:12]
        self._messages: list[dict] = []
        self._emitted = self._cancelled = self._terminated = False

    async def __aenter__(self):
        await self._open()
        return self

    async def _open(self) -> None:
        s = self.tts._settings
        self._stripper = _WavStripper() if self.tts._format == "wav" else None
        # Sunucu kapanış el sıkışmasına ~1,1 sn sonra cevap veriyor (ölçüm, 22 Ekim 2026);
        # ses o ana kadar tamamen gelmiş olur. Beklenirse cümle cümle konuşmada her cümle
        # arasında boşluk oluşuyordu.
        self._connection = websockets.connect(
            s.soniox_endpoint(s.tts_ws_url),
            additional_headers={"Authorization": f"Bearer {s.soniox_api_key}"},
            max_size=None,
            close_timeout=0.1,
        )
        self._ws = await self._connection.__aenter__()
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "stream_id": self.stream_id,
                        "model": s.tts_model,
                        "language": self.language,
                        "voice": s.tts_voice,
                        "audio_format": self.tts._format,
                        "sample_rate": s.tts_sample_rate,
                        "return_timestamps": True,
                    }
                )
            )
        except BaseException:
            await self._connection.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *exc):
        return await self._connection.__aexit__(*exc)

    async def send(self, text: str, *, end: bool = False) -> None:
        for start in range(0, max(1, len(text)), 5000):
            message = {
                "stream_id": self.stream_id,
                "text": text[start : start + 5000],
                "text_end": end and start + 5000 >= len(text),
            }
            self._messages.append(message)
            await self._ws.send(json.dumps(message))

    async def cancel(self) -> None:
        if not self._cancelled and not self._terminated:
            self._cancelled = True
            await self._ws.send(json.dumps({"stream_id": self.stream_id, "cancel": True}))

    def __aiter__(self) -> AsyncIterator[TTSEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[TTSEvent]:
        while True:
            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("error_code") is not None:
                    if (
                        not self._emitted
                        and not self._cancelled
                        and self.tts._format == "pcm_s16le"
                        and msg["error_code"] == 400
                    ):
                        log.info("ham PCM reddedildi, wav'a geçiliyor")
                        await self._connection.__aexit__(None, None, None)
                        self.tts._format = "wav"
                        await self._open()
                        for message in self._messages:
                            await self._ws.send(json.dumps(message))
                        break
                    raise TTSError(msg["error_code"], msg.get("error_message", ""))
                if audio := msg.get("audio"):
                    data = base64.b64decode(audio)
                    if self._stripper:
                        data = self._stripper.feed(data)
                    self._emitted |= bool(data)
                    yield TTSEvent(data, msg.get("timestamps"))
                if msg.get("terminated"):
                    self._terminated = True
                    return
            else:
                raise ConnectionError("TTS bağlantısı ses bitmeden kapandı")


def apply_lexicon(text: str, lexicon: dict[str, str]) -> str:
    """Telaffuz sözlüğünü uygular (tam kelime, büyük/küçük harf duyarsız).

    Türkçe ekler korunur: "Example'in" → "Eksampıl'in".
    """
    for written, spoken in sorted(lexicon.items(), key=lambda kv: len(kv[0]), reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(written)}(?!\w)", spoken, text, flags=re.IGNORECASE)
    return text
