"""Soniox gerçek zamanlı STT + çeviri adaptörü (ham WebSocket).

Sözleşme (https://soniox.com/docs/stt/api-reference/websocket-api):
  1. Bağlan, ilk mesaj olarak JSON config gönder.
  2. Sesi binary frame olarak akıt; bitişte boş frame gönder.
  3. Sunucu `{"tokens": [...]}` mesajları döner. Final token'lar bir kez gelir;
     final olmayanlar her mesajda baştan gönderilir (önceki kuyruğun yerini alır).
     Konuşma sonu `<end>` (endpoint detection) ya da `<fin>` (manuel finalize)
     token'ı ile işaretlenir. Çeviri token'ları `translation_status == "translation"`
     ile aynı akışta, orijinallerin arkasından gelir ve zaman damgası taşımaz.
  4. Ses gelmezse 20 sn'de bağlantı düşer → `{"type": "keepalive"}`.

Faturalama stream'in açık kaldığı süre üzerindendir; bu yüzden toplantı başına
tek (karışık) akış açıyoruz.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

import websockets

from katip.async_utils import wait_tasks
from katip.config import Settings, get_settings
from katip.logging import get_logger
from katip.pipeline.stt.base import STTProvider, Utterance
from katip.providers.interfaces import Capabilities
from katip.providers.wire import messages

log = get_logger(__name__)

_END_TOKENS = frozenset({"<end>", "<fin>"})
_KEEPALIVE_AFTER_S = 10.0


class SonioxError(RuntimeError):
    def __init__(self, code: int | str | None, message: str) -> None:
        super().__init__(f"Soniox hatası {code}: {message}")
        self.code = code


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class _Building:
    """Henüz `<end>` görmemiş, inşa edilmekte olan konuşma parçası."""

    id: str = field(default_factory=_new_id)
    final_tokens: list[dict] = field(default_factory=list)
    final_translation: list[str] = field(default_factory=list)

    @property
    def speaker(self) -> str | None:
        labels = [t.get("speaker") for t in self.final_tokens if t.get("speaker") is not None]
        return str(Counter(labels).most_common(1)[0][0]) if labels else None


class SonioxAssembler:
    """Soniox token akışını `Utterance` güncellemelerine çevirir. Ağdan bağımsız, saf mantık.

    Kurallar:
    - Final orijinal token'lar mevcut parçaya eklenir.
    - Diarization konuşmacıyı değiştirirse mevcut parça kapatılır, yenisi açılır.
    - `<end>`/`<fin>` mevcut parçayı kesinleştirir.
    - Kesinleşen parçanın çevirisi `<end>`'den sonra da gelebilir: yeni bir orijinal
      final token gelene kadar gelen final çeviri token'ları önceki parçaya yazılır.
    """

    def __init__(self, language: str, target_language: str | None) -> None:
        self._language = language
        self._target = target_language
        self._cur = _Building()
        self._prev: Utterance | None = None
        self._last_partial: tuple[str, str] | None = None
        self._prev_dirty = False

    # --------------------------------------------------------------- yardımcılar
    def _utterance(
        self, b: _Building, extra_tokens: list[dict], extra_translation: list[str], final: bool
    ) -> Utterance:
        tokens = b.final_tokens + extra_tokens
        timed = [t for t in tokens if t.get("start_ms") is not None]
        start = timed[0]["start_ms"] / 1000 if timed else 0.0
        end = timed[-1].get("end_ms", timed[-1]["start_ms"]) / 1000 if timed else start
        langs = [t.get("language") for t in tokens if t.get("language")]
        return Utterance(
            id=b.id,
            text="".join(t["text"] for t in tokens).strip(),
            is_final=final,
            start_s=start,
            end_s=end,
            speaker_label=b.speaker
            or next((str(t["speaker"]) for t in tokens if t.get("speaker") is not None), None),
            lang=langs[0] if langs else self._language,
            translation="".join(b.final_translation + extra_translation).strip(),
            translation_lang=self._target,
        )

    def _release_prev(self, out: list[Utterance]) -> None:
        """Önceki parçayı bırakır; geç çeviriyle güncellendiyse son hâlini yayınlar."""
        if self._prev_dirty and self._prev is not None:
            out.append(self._prev)
        self._prev = None
        self._prev_dirty = False

    def _close_current(self, out: list[Utterance]) -> None:
        if not self._cur.final_tokens:
            return
        self._release_prev(out)
        done = self._utterance(self._cur, [], [], final=True)
        out.append(done)
        self._prev = done
        self._cur = _Building()
        self._last_partial = None

    # ------------------------------------------------------------------ arayüz
    def feed(self, message: dict) -> list[Utterance]:
        out: list[Utterance] = []
        partial_tokens: list[dict] = []
        partial_translation: list[str] = []

        for tok in message.get("tokens", []):
            text = tok.get("text", "")
            is_final = bool(tok.get("is_final"))

            if text in _END_TOKENS:
                self._close_current(out)
                continue

            if tok.get("translation_status") == "translation":
                if not is_final:
                    partial_translation.append(text)
                elif not self._cur.final_tokens and self._prev is not None:
                    # Önceki parçanın geç gelen çevirisi.
                    self._prev = replace(
                        self._prev, translation=(self._prev.translation + text).strip()
                    )
                    self._prev_dirty = True
                else:
                    self._cur.final_translation.append(text)
                continue

            if not is_final:
                partial_tokens.append(tok)
                continue

            speaker = tok.get("speaker")
            cur_speaker = self._cur.speaker
            if speaker is not None and cur_speaker is not None and str(speaker) != cur_speaker:
                self._close_current(out)
            # Yeni orijinal konuşma başladı: önceki parçanın çevirisi artık kapandı.
            self._release_prev(out)
            self._cur.final_tokens.append(tok)

        if self._prev_dirty and self._prev is not None:
            out.append(self._prev)
            self._prev_dirty = False

        if self._cur.final_tokens or partial_tokens:
            partial = self._utterance(self._cur, partial_tokens, partial_translation, final=False)
            key = (partial.text, partial.translation)
            if key != self._last_partial:
                self._last_partial = key
                out.append(partial)
        return out

    def flush(self) -> list[Utterance]:
        """Akış bittiğinde elde kalan final token'ları kesinleştirir."""
        out: list[Utterance] = []
        self._close_current(out)
        self._release_prev(out)
        return out


class SonioxSTT(STTProvider):
    translates = True
    name = "soniox"
    capabilities = Capabilities(translation=True, diarization=True)

    @property
    def region(self):
        return self._settings.soniox_region

    @property
    def model(self):
        return self._settings.soniox_model

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.soniox_api_key:
            raise RuntimeError("SONIOX_API_KEY tanımlı değil")

    def _config(self, language: str, target_language: str | None) -> dict:
        s = self._settings
        config: dict = {
            "api_key": s.soniox_api_key,
            "model": s.soniox_model,
            "audio_format": "pcm_s16le",
            "sample_rate": s.sample_rate,
            "num_channels": 1,
            "language_hints": [language],
            "enable_endpoint_detection": True,
            "max_endpoint_delay_ms": s.soniox_max_endpoint_delay_ms,
            "enable_speaker_diarization": True,
            "enable_language_identification": True,
        }
        if s.stt_terms:
            config["context"] = {"terms": s.stt_terms}
        if target_language and target_language != language:
            config["translation"] = {"type": "one_way", "target_language": target_language}
        return config

    async def stream(
        self,
        audio: AsyncIterator[bytes],
        *,
        language: str,
        target_language: str | None = None,
    ) -> AsyncIterator[Utterance]:
        assembler = SonioxAssembler(language, target_language)

        async with websockets.connect(
            self._settings.soniox_endpoint(self._settings.soniox_ws_url), max_size=None
        ) as ws:
            await ws.send(json.dumps(self._config(language, target_language)))
            last_audio = time.monotonic()

            async def pump() -> None:
                nonlocal last_audio
                try:
                    async for chunk in audio:
                        await ws.send(chunk)
                        last_audio = time.monotonic()
                finally:
                    # Boş *metin* frame'i = ses bitti; Soniox kalan final'leri gönderip
                    # "finished" ile kapatır. (Boş binary frame yok sayılıyor — canlı
                    # API'de doğrulandı, akış açık kalıyordu.)
                    with contextlib.suppress(websockets.ConnectionClosed):
                        await ws.send("")

            async def keepalive() -> None:
                while True:
                    await asyncio.sleep(_KEEPALIVE_AFTER_S / 2)
                    if time.monotonic() - last_audio >= _KEEPALIVE_AFTER_S:
                        await ws.send(json.dumps({"type": "keepalive"}))

            tasks = [asyncio.create_task(pump()), asyncio.create_task(keepalive())]
            try:
                async for raw in messages(ws, tasks[0], tasks[1]):
                    msg = json.loads(raw)
                    if msg.get("error_code") is not None:
                        raise SonioxError(msg.get("error_code"), msg.get("error_message", ""))
                    for utt in assembler.feed(msg):
                        yield utt
                    if msg.get("finished"):
                        break
                for utt in assembler.flush():
                    yield utt
            finally:
                await wait_tasks(tasks, budget_s=1, label="Soniox göndericileri", cancel=True)
                for task in tasks:
                    if task.done():
                        with contextlib.suppress(
                            asyncio.CancelledError, websockets.ConnectionClosed
                        ):
                            task.result()
