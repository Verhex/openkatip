"""AssemblyAI Universal-3.6 Pro: EU, Türkçe, kod değişimi ve konuşmacı ayrımı.

Context7: assemblyai.com/docs/streaming/api-spec/streaming-websocket ve
streaming/label-speakers-and-separate-channels. Çeviri LLM katmanındadır.
"""

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets

from katip.async_utils import wait_tasks
from katip.config import Settings, get_settings
from katip.pipeline.stt.base import StreamExpired, STTProvider, Utterance
from katip.providers.interfaces import Capabilities
from katip.providers.wire import messages


class AssemblyAIError(RuntimeError):
    """Belgelenen akış kodları: 3005 sunucu hatası, 3009 eşzamanlı oturum sınırı."""

    def __init__(self, code: int | None):
        self.status_code = {3005: 500, 3009: 429, 1011: 500}.get(code, 400)
        super().__init__("AssemblyAI akışı sonlandırıldı")


class AssemblyAISTT(STTProvider):
    name = "assemblyai"
    region = "eu"
    model = "universal-3-6-pro"
    capabilities = Capabilities(diarization=True)

    def __init__(self, *, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self.audio_backlog = lambda: True
        if not self._settings.assemblyai_api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY tanımlı değil")

    def _url(self):
        return "wss://streaming.eu.assemblyai.com/v3/ws?" + urlencode(
            {
                "sample_rate": self._settings.sample_rate,
                "speech_model": self.model,
                "speaker_labels": "true",
                "language_detection": "true",
            }
        )

    @staticmethod
    def turn(msg: dict, prefix: str, language: str) -> list[Utterance]:
        if msg.get("type") != "Turn" or not msg.get("transcript"):
            return []
        words = msg.get("words", [])
        # Sözcük düzeyindeki etiket, tek bir turn içindeki konuşmacı değişimini korur.
        groups: list[tuple[str | None, list[dict]]] = []
        for word in words:
            label = word.get("speaker") or msg.get("speaker_label")
            if groups and groups[-1][0] == label:
                groups[-1][1].append(word)
            else:
                groups.append((label, [word]))
        if not groups:
            groups = [(msg.get("speaker_label"), [])]
        return [
            Utterance(
                id=f"{prefix}-{msg['turn_order']}-{index}",
                text=(
                    msg["transcript"] if len(groups) == 1 else " ".join(w["text"] for w in group)
                ),
                is_final=bool(msg.get("end_of_turn")),
                start_s=group[0]["start"] / 1000 if group else 0,
                end_s=group[-1]["end"] / 1000 if group else 0,
                speaker_label=label,
                lang=msg.get("language_code") or language,
            )
            for index, (label, group) in enumerate(groups)
        ]

    async def stream(
        self, audio: AsyncIterator[bytes], *, language: str, target_language: str | None = None
    ) -> AsyncIterator[Utterance]:
        s = self._settings
        prefix = uuid.uuid4().hex[:12]
        async with websockets.connect(
            self._url(), additional_headers={"Authorization": s.assemblyai_api_key}, max_size=None
        ) as ws:

            async def pump():
                pending = b""
                next_t = time.monotonic()
                # Belgelenen paket aralığı 50–1000 ms; 20 ms girişler 100 ms'de birleşir.
                step = s.sample_rate * 2 // 10
                async for chunk in audio:
                    pending += chunk
                    while len(pending) >= step:
                        # Giriş beklemesi saatten sayılır; send gecikmesi birikmez.
                        if time.monotonic() - next_t > 0.1:
                            next_t = time.monotonic()
                        await asyncio.sleep(max(0, next_t - time.monotonic()))
                        await ws.send(pending[:step])
                        pending = pending[step:]
                        # Belgelenen işlem hızı yaklaşık 1,25×; tampon bu hızda boşalır.
                        speed = 1.25 if pending or self.audio_backlog() else 1.0
                        next_t += 0.1 / speed
                if pending:
                    minimum = s.sample_rate * 2 // 20
                    await asyncio.sleep(max(0, next_t - time.monotonic()))
                    await ws.send(pending.ljust(minimum, b"\0"))
                await ws.send(json.dumps({"type": "Terminate"}))

            task = asyncio.create_task(pump())
            try:
                async for raw in messages(ws, task):
                    msg = json.loads(raw)
                    if msg.get("type") == "Termination":
                        return
                    if msg.get("type") == "Error":
                        if msg.get("error_code") == 3008:
                            raise StreamExpired()
                        raise AssemblyAIError(msg.get("error_code"))
                    for utt in self.turn(msg, prefix, language):
                        yield utt
            except websockets.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code == 3008:
                    raise StreamExpired() from None
                if code in (3005, 3009):
                    raise AssemblyAIError(code) from None
                raise
            except TimeoutError:
                # Ses sonundan sonraki sessiz sunucu normal kapanış sayılır.
                if task.done() and not task.cancelled() and task.exception() is None:
                    return
                raise
            finally:
                await wait_tasks({task}, budget_s=1, label="AssemblyAI göndericisi", cancel=True)
                if task.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        task.result()
